# 我的 Rime 配置

macOS 上鼠须管的用户目录 (`~/Library/Rime`). 词库与 Lua 脚本来自 [万象拼音](https://github.com/amzxyz/rime_wanxiang), 配置按个人习惯做过裁剪.

## 当前状态

- 只保留**小鹤双拼**, `wanxiang_algebra.yaml` 里其他双拼与全拼的定义已全部移除.
- 启用方案只有 `wanxiang`, 候选 4 个.
- 中英切换: `Shift_R` 切英文, `Alt_R` 切回中文, 组字状态下切换会先把已输入的编码上屏.
- 反查, 中英混输, 英文分别由 `wanxiang_reverse`, `wanxiang_mixedcode`, `wanxiang_english` 承担, 都固定为小鹤双拼.
- `custom/wanxiang_pro.schema.yaml` 增强版方案保留备用, 同样指向小鹤双拼.

## 与上游的关系

本仓库是个人配置, 不跟随上游更新, 也不合并上游分支. 上游资料仅作参考:

- 文档: <https://amzxyz.github.io/>
- 仓库: <https://github.com/amzxyz/rime_wanxiang>

给 AI 助手的约定写在 `README-azazo1.md`.

## 目录

| 路径 | 说明 |
| --- | --- |
| `wanxiang.schema.yaml` | 主方案, 输入引擎与开关 |
| `wanxiang_algebra.yaml` | 拼写运算, 只含小鹤双拼 |
| `wanxiang.custom.yaml` | 主方案的个人覆盖 |
| `wanxiang_*.custom.yaml` | 各子方案的覆盖文件 |
| `wanxiang.dict.yaml`, `dicts/` | 词库入口与词库文件 |
| `lua/wanxiang/` | Lua 扩展脚本 |
| `custom/` | 拆分表与增强版方案素材 |
| `build/`, `*.userdb/` | 部署产物与用户词典, 不纳入版本控制 |

## 日常操作

改完配置后重新部署:

```shell
"/Library/Input Methods/Squirrel.app/Contents/MacOS/Squirrel" --reload
```

也可以在鼠须管菜单里点"重新部署". 回滚某个文件:

```shell
git log --oneline
git checkout <commit> -- <path>
```

## 版权

词库与 Lua 脚本的版权归万象拼音项目所有, 详见 `LICENSE`.
