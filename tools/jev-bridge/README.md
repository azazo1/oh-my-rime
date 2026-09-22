# jev-bridge

给 Rime 万象拼音的候选重排提供服务的本地 sidecar: 接收 Rime Lua 投递的"上文 + 编码 + 候选列表",
交给 typed-decision 模型 (云端 Jev 或本地 Laya/MLX) 打分, 把概率映射回候选顺序, 结果既回给 Lua,
也落进磁盘缓存供 Lua 直接读取.

候选重排只是第一种用法; 整句优选, 联想重排, 离线词库整理等后续能力的设计见仓库根目录的 `todo.md`.

## 为什么需要 sidecar

librime-lua 只内建 `utf8`, 没有 socket 也没有 HTTP 客户端, 而 Jev 云端延迟 70-500ms,
在按键路径里同步等待必然卡手. 所以拆成两层:

```
Squirrel / librime
  lua/jev/jev_filter.lua        只读缓存, 未命中就投递一个请求然后立刻返回 (默认 async)
        │  <queue_dir>/<id>.req.json      (先写 .tmp 再 rename)
        │  <queue_dir>/<id>.res.json
        │  <cache_dir>/<key>.json         (键 = FNV-1a64, Lua 与 Python 两侧同算法)
        ▼
tools/jev-bridge (本项目)
  queue.py     轮询队列 (15ms), 同键请求合并, 过期预取丢弃, 孤儿文件清理
  rerank.py    概率 -> 顺序的映射与门限 (置信度, 首选概率)
  cache.py     TTL + LRU 磁盘缓存
  backends/    mock | http (Jev 兼容的 /v1/systemone: localjev-mlx 或 api.typesafe.ai)
  server.py    http://127.0.0.1:20006/health  仅用于调试
```

运行数据全部在仓库之外: `~/Library/Caches/rime-jev/{queue,cache,log,backup}`。

## 快速开始 (mock 后端, 不需要任何 key 与模型)

```shell
cd ~/Library/Rime/tools/jev-bridge
just install              # uv sync, 建 .venv
just init-config          # 把 config.toml.example 复制成 ~/.config/rime-jev/config.toml
just run                  # 前台跑 sidecar, 保持这个终端开着
```

另开一个终端验证链路:

```shell
cd ~/Library/Rime/tools/jev-bridge
just health                                   # 应当返回 ok: true, backend: mock
just simulate nihao "今天天气"                 # 投递一次请求并打印响应 JSON
just patch-dry                                # 看 config 补丁会改什么
just patch-config                             # 真正写入 wanxiang.custom.yaml
just check-config                             # 校验合并后的 YAML
just deploy-rime                              # 鼠须管重新部署
```

然后在任意输入框里按 `Control+Shift+J` 打开 `AI开`, 打字观察首位候选注释是否出现 `AI`,
以及候选顺序是否按分数变化. 关掉开关或停掉 sidecar 后行为必须与改动前完全一致.

## 配置

两层配置, 各管一摊:

| 位置 | 管什么 |
| --- | --- |
| `config.toml.example` | 仓库里的示例配置, 每一项都带注释, 也是 `just init-config` 的唯一来源 |
| `~/.config/rime-jev/config.toml` | 实际生效的配置: 后端地址, 队列/缓存/端口, 超时, 缓存 TTL, 门限, 日志级别 |
| schema 的 `jev_rerank:` 段 (由补丁写进 `wanxiang.custom.yaml`) | 开关行为: mode, 等待预算, 候选上限, 上下文长度, 标记文本, 方案白名单 |

调试端点默认在 `http://127.0.0.1:20006` (`http_host` / `http_port`), 打字路径不经过它;
`base_url` 里的端口是**后端自己监听**的端口 (默认 `8090`, 与 localjev-mlx 的默认值一致),
和 sidecar 的调试端口 `20006` 是两件事。

`config.toml` 的所有项都能被同名环境变量覆盖: `JEV_BACKEND`, `JEV_BASE_URL`, `JEV_MODEL`,
`JEV_ALLOW_CLOUD`, `JEV_RUNTIME_DIR`, `JEV_HTTP_HOST`, `JEV_HTTP_PORT`, `JEV_LOG_LEVEL`, `JEV_DEBUG`, `TYPESAFE_API_KEY`。
用 `just config` 看最终生效值. 配置带 `config_version`, 升级走 `config_migrations.py`, 不做隐式兼容.

### 后端

| backend | 说明 | 延迟感受 |
| --- | --- | --- |
| `mock` | 零依赖, 确定性规则造分, 只用于验证链路 | 微秒级 |
| `http` + `http://127.0.0.1:8090` | localjev-mlx / laya-mlx (Laya 权重 + MLX), 本机推理 | M3 Max 上 P50 7-14ms, M1 上需实测 |
| `http` + `https://api.typesafe.ai` | 官方 Jev 云端, 需要 key | 70-500ms, 只能 async |

云端后端会把上文送出本机, 因此 `base_url` 指向非本机地址时**必须显式** `allow_cloud = true`,
否则配置校验直接报错, 并且后端在运行时也会拒绝调用。

### 两种模式

- `async` (默认): 过滤器只读缓存, 未命中就投递预取请求后立刻返回原顺序。下一次刷新同一编码时命中缓存,
  顺序立即生效。按键路径零等待, 代价是首次输入看不到重排。
- `sync`: 未命中时投递并等待 `timeout_ms`, 超时同样原序放行, 结果稍后进缓存。
  只有后端 ≲30ms 时才不卡手; 云端 Jev 和本机 Laya 都达不到, 所以默认是 async。

实测参考 (Apple M1, `convaiinnovations/laya` 421M, 单问一答): **P50 89.7ms / P95 170.7ms**,
首次调用还要额外预热 (实测 >800ms, 因此 `backend_timeout_ms` 默认放宽到 3000ms)。
想换更省时的 checkpoint 就调整 `just laya` 的 `--repo` / `--subfolder` (例如上游的 `multilingual`)。

## 命令

```shell
just --list          # 全部 recipe
just test            # Python (pytest) + Lua 纯逻辑测试
just run             # 前台运行
just logs            # 看 sidecar 日志
just bench 20        # 延迟基准 (当前后端, 冷缓存)
just patch-config    # 写入 Rime 配置补丁
just unpatch-config  # 移除补丁
just deploy-rime     # 鼠须管重新部署 (仅 macOS)
just install-agent   # 可选: 装成 LaunchAgent 常驻 (仅 macOS, 写 ~/Library/LaunchAgents)
```

## Windows / Linux

sidecar 是纯 Python 标准库实现 (Python 3.12+), 三个平台都能直接前台启动, 不需要编译:

```shell
git clone <本仓库> && cd <本仓库>/tools/jev-bridge
uv sync
uv run jev-bridge serve          # 或 uv run jev-bridge once 只处理一轮
```

**路径不用手填**, 全部按平台自动推导, 两侧用的是同一套规则:

| 需要的东西 | 推导规则 |
| --- | --- |
| sidecar 的队列/缓存/日志 | `<runtime_dir>/{queue,cache,log}`, `runtime_dir` 默认 macOS `~/Library/Caches/rime-jev`, Linux `$XDG_CACHE_HOME/rime-jev` 或 `~/.cache/rime-jev`, Windows `%LOCALAPPDATA%\rime-jev` |
| Rime 侧的 `jev_rerank/runtime_dir` | `patch-config` 按同一规则算好后**写进** `wanxiang.custom.yaml` (HOME 之下写成 `~/...`, 换机器仍可用), Rime 侧不需要自己猜平台 |
| Rime 用户目录 | macOS `~/Library/Rime`; Linux 依次找 `~/.local/share/fcitx5/rime`, `~/.config/ibus/rime`, `~/.local/share/fcitx/rime`; Windows `%APPDATA%\Rime` |

只有想覆盖默认值时才需要动手: 环境变量 `JEV_RUNTIME_DIR` / `RIME_USER_DIR`, schema 里的 `jev_rerank/runtime_dir`,
或 `patch-config --rime-dir`。`just paths` 会打印推导结果, 并核对两侧 runtime_dir 是否一致 (不一致时非零退出)。

Lua 侧 `runtime_dir` 支持 `~` 展开, Windows 下建目录会自动换成 `cmd.exe` 的写法, 不需要额外处理。

平台专属的部分只有 **常驻方式**: `just install-agent` 用的是 launchd (macOS)。
Linux 直接抄 `systemd/jev-bridge.service` (文件末尾写了安装命令), Windows 用计划任务或 `nssm`,
内容都是同一条 `jev-bridge serve`;
不装常驻服务时前台运行也完全可用, sidecar 不在线时过滤器只花 0.2ms 跳过, 不影响打字。

### 本机 Laya 后端的隔离启动

不要跑 `localjev-mlx` 的 `install.sh`: 它会建 `~/.localjev-mlx`, 写 `~/.config/localjev-mlx`,
装 launchd 常驻服务, 还会往 `~/.claude` / `~/.codex` / `~/.cursor` / `~/.grok` 里塞它的 skill。
它本身只是个普通 Python 包, 我们直接用 `uv tool run` 在临时环境里跑:

```shell
cd ~/Library/Rime/tools/jev-bridge
just laya              # 前台跑, Ctrl-C 退出
just laya-bg           # 后台跑并等 /health 变 200 (首次要下载权重)
just laya-status       # 看状态 (pid + health)
just laya-stop         # 停掉
```

隔离体现在:

| 项目 | 落点 |
| --- | --- |
| 模型权重 | huggingface-hub 的默认缓存 `~/.cache/huggingface` (想换位置自己设 `HF_HOME=... just laya`) |
| 进程号 / 日志 | `<runtime_dir>/laya.pid`, `<runtime_dir>/log/laya.log` |
| Python 环境 | uv 自己的工具缓存 (`uv tool run`), 不进项目 venv; 需要时 `uv cache clean` 回收 |
| 系统服务 / 家目录配置 | **不写** (没有 LaunchAgent, 没有 `~/.config/localjev-mlx`, 不动 `~/.claude` 等目录) |

默认端口 8090 (`--port` 可改), 与 `config.toml` 里的 `base_url` 默认值一致;
请求体里的 `model` 字段它不校验, 返回的 `model` 是它加载的 HF 仓库名。
把 sidecar 切到它:

```toml
backend = "http"
base_url = "http://127.0.0.1:8090"
```

然后 `just config` 确认、重启 sidecar、`just bench 20` 看延迟。

### Windows 上怎么真正用上 Jev

`localjev-mlx` / `laya-mlx` 走的是 Apple MLX, **Windows 上没有本地后端可用**, 所以 Windows 只有云端一条路
(或任何自己实现 `POST /v1/systemone` 的服务)。需要改的就三行:

```toml
# %USERPROFILE%\.config\rime-jev\config.toml
backend = "http"
base_url = "https://api.typesafe.ai"     # 官方; 也可换成自建的同协议服务
model = "jev-latest"
allow_cloud = true                        # 必须显式打开, 云端会收到你正在输入的上文
```

api_key 建议走环境变量而不是写进文件: PowerShell 里 `$env:TYPESAFE_API_KEY = "..."`,
要持久化就 `setx TYPESAFE_API_KEY "..."` (重开终端生效)。

schema 侧的 `jev_rerank/mode` 要改成 `async` (或把 `timeout_ms` 压到 10 左右): 云端延迟 70-500ms,
`sync` 只会让每次按键白等一个超时。async 的代价是首次输入某个编码看不到重排, 同一个编码第二次出现时才生效。

链路本身与平台无关, sidecar 每次按键做的事就是发这样一次请求, 再按返回的概率重排候选:

```json
POST https://api.typesafe.ai/v1/systemone
{
  "state": {"context": "今天天气不错", "input": "nihao",
            "candidates": [{"id": "0", "text": "你好"}, {"id": "1", "text": "尼豪"}, {"id": "2", "text": "拟好"}]},
  "model": "jev-latest",
  "questions": {"best_continuation": {"type": "choice",
      "instructions": "...choose which candidate the user most likely intends...",
      "criteria": {"0": "你好", "1": "尼豪", "2": "拟好"}}}
}
```

返回 `answers.best_continuation.probabilities` 与 `confidence`; 置信度或首选概率不达门限就保持原顺序,
只加 `AI` 标记。

## 排障

| 现象 | 排查方向 |
| --- | --- |
| 候选没有 `AI` 标记 | 开关是否 `AI开`; `jev_rerank/debug = true` 后看 `~/Library/Caches/rime-jev/log/lua.log` 的跳过原因 |
| 有标记但顺序从不变化 | 看 sidecar 日志的 `置信=` 与 `顺序变化=`; 门限见 `config.toml` 的 `min_confidence` / `min_top_prob` |
| 每次都是未命中 | 停掉 sidecar 时投递成功但无人消费属正常; 侧车在跑却持续未命中则看日志里的"缓存键不一致"告警 |
| 打字变卡 | 当前很可能是 `sync` 模式且后端慢; 换成 `async`, 或把 `timeout_ms` 调小 |
| 云端被拒 | `allow_cloud` 未开, 这是有意为之的安全默认 |
| 端口占用 | 改 `config.toml` 的 `http_port`, 或只用文件队列 (HTTP 端点只是调试用) |

日志位置: `~/Library/Caches/rime-jev/log/sidecar.log` (轮转, 2MB x 3)。

## 测试

```shell
just test        # 全部
just test-py     # pytest: 键/缓存/重排/队列/HTTP/补丁
just test-lua    # lua 解释器跑 lua/jev 的纯逻辑与文件队列
```

两侧共享同一份缓存键向量 `tests/fixtures/key_vectors.json`: Lua 与 Python 任一方的归一化规则改动
都会让对方的测试失败, 避免出现"缓存永远不命中"这种难查的线上问题。
