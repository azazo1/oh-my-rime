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
  server.py    http://127.0.0.1:8091/health  仅用于调试
```

运行数据全部在仓库之外: `~/Library/Caches/rime-jev/{queue,cache,log,backup}`。

## 快速开始 (mock 后端, 不需要任何 key 与模型)

```shell
cd ~/Library/Rime/tools/jev-bridge
just install              # uv sync, 建 .venv
just init-config          # 生成 ~/.config/rime-jev/config.toml (默认 backend = mock)
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
| `~/.config/rime-jev/config.toml` | 后端地址, 队列/缓存/端口, 超时, 缓存 TTL, 门限, 日志级别 |
| schema 的 `jev_rerank:` 段 (由补丁写进 `wanxiang.custom.yaml`) | 开关行为: mode, 等待预算, 候选上限, 上下文长度, 标记文本, 方案白名单 |

`config.toml` 的所有项都能被同名环境变量覆盖: `JEV_BACKEND`, `JEV_BASE_URL`, `JEV_MODEL`,
`JEV_ALLOW_CLOUD`, `JEV_HTTP_PORT`, `JEV_LOG_LEVEL`, `JEV_DEBUG`, `TYPESAFE_API_KEY`。
用 `just config` 看最终生效值. 配置带 `config_version`, 升级走 `config_migrations.py`, 不做隐式兼容.

### 后端

| backend | 说明 | 延迟感受 |
| --- | --- | --- |
| `mock` | 零依赖, 确定性规则造分, 只用于验证链路 | 微秒级 |
| `http` + `http://127.0.0.1:8090` | localjev-mlx (Laya 权重 + MLX), 本机推理 | M3 Max 上 P50 7-14ms, M1 上需实测 |
| `http` + `https://api.typesafe.ai` | 官方 Jev 云端, 需要 key | 70-500ms, 只能 async |

云端后端会把上文送出本机, 因此 `base_url` 指向非本机地址时**必须显式** `allow_cloud = true`,
否则配置校验直接报错, 并且后端在运行时也会拒绝调用。

### 两种模式

- `async` (默认): 过滤器只读缓存, 未命中就投递预取请求后立刻返回原顺序。下一次刷新同一编码时命中缓存,
  顺序立即生效。按键路径零等待, 代价是首次输入看不到重排。
- `sync`: 未命中时投递并等待 `timeout_ms`, 超时同样原序放行, 结果稍后进缓存。
  适合本地 Laya 这类毫秒级后端; 云端后端请勿使用。

## 命令

```shell
just --list          # 全部 recipe
just test            # Python (pytest) + Lua 纯逻辑测试
just run             # 前台运行
just logs            # 看 sidecar 日志
just bench 20        # 延迟基准 (当前后端, 冷缓存)
just patch-config    # 写入 Rime 配置补丁
just unpatch-config  # 移除补丁
just deploy-rime     # 鼠须管重新部署
just install-agent   # 可选: 装成 LaunchAgent 常驻 (写 ~/Library/LaunchAgents)
```

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
