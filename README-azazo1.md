# 个人基于万象拼音的 rime 配置

## 个人配置步骤

我会先在这个配置里全局搜索和这三项相关的键（候选词数量、英文标点、Shift 切换），定位到你项目里实际生效的位置，然后给你可直接改的配置片段。先从 `schema`、`weasel` 和 custom 覆盖文件里查。

Searched for regex `page_size|候选|menu|candidate|候选词` (`**/*.yaml`), 20 results

Searched for regex `punctuator|标点|ascii_punct|full_shape|half_shape|symbols` (`**/*.yaml`), 20 results

Searched for regex `switch_key|Shift|shift|ascii_mode|中英文|ascii_composer` (`**/*.yaml`), 20 results

Read memory

我已经定位到关键项了，下一步读取这些文件的上下文，确认这个项目推荐的覆盖写法（尤其是 `.custom.yaml`）再给你最稳妥的改法。

Read [](file:///Users/azazo1/Library/Rime/weasel.yaml#1-1), lines 1 to 45

Read [](file:///Users/azazo1/Library/Rime/default.yaml#50-50), lines 50 to 95

Read [](file:///Users/azazo1/Library/Rime/wanxiang.custom.yaml#1-1), lines 1 to 80

Read [](file:///Users/azazo1/Library/Rime/wanxiang.schema.yaml#1-1), lines 1 to 40

Read [](file:///Users/azazo1/Library/Rime/wanxiang.schema.yaml#580-580), lines 580 to 680

我再补查一下 macOS 前端相关文件（`squirrel`），避免把 Windows 专用的 `weasel` 设置误给你，确保你现在这台机器能直接生效。

Searched for regex `ascii_punct|ascii_mode|app_options|switch_key|Shift_L|Shift_R` (`**/squirrel.yaml`), 17 results

Searched for files matching `**/*.custom.yaml`, 9 matches

Read [](file:///Users/azazo1/Library/Rime/README.md#1-1), lines 1 to 220

我已经确认到三项都在可覆盖配置里了，最后再抓一下精确行号，给你一份可以直接照改的最小步骤。

Searched for text `menu/page_size:` (`**/wanxiang.custom.yaml`), 2 results

Searched for regex `ascii_composer:|switch_key:|Shift_L:|Shift_R:` (`**/default.yaml`), 8 results

Searched for regex `toggle: ascii_punct|name: ascii_punct|states: \[ 中标, 英标 \]` (`**/wanxiang.schema.yaml`), 7 results

Searched for text `good_old_caps_lock` (`**/default.yaml`), 3 results

Searched for regex `切勿|custom/ 文件夹|修改 custom` (`**/README.md`), 1 result

可以，已经帮你在这个项目里定位到了，按下面改就行。

1. 候选词数量改为 4  
你现在用的是 wanxiang.custom.yaml，当前值是 6。  
把这一行改成：

menu/page_size: 4

2. 英文标点设置  
这个方案已经有中标/英标开关定义，在 wanxiang.schema.yaml 和 wanxiang.schema.yaml。  
快捷键也已经配好，在 wanxiang.schema.yaml 和 wanxiang.schema.yaml：
Control+Shift+3 可切换中标/英标。

如果你想默认就是英标，可以在 wanxiang.custom.yaml 的 patch 下加一行：

switches/@1/reset: 1

说明：第 2 个开关是 ascii_punct，states 是 [中标, 英标]，reset: 1 就是默认英标。

3. 取消 Shift 的中英文切换  
在 default.yaml 的 ascii_composer 里改 default.yaml 和 default.yaml：

Shift_L: noop  
Shift_R: noop

你当前是 commit_code，所以按一下 Shift 会参与中英切换逻辑。改成 noop 就禁用了。

补充两点  
1. 你是 macOS，外观前端文件通常是 squirrel；但这三项里，候选数和标点在 schema/custom，Shift 切换在 default，属于通用层，不是 weasel 专属。  
2. 不要改 custom 目录里的模板文件，README 也明确写了这一点，见 README.md。应改根目录下的 wanxiang.custom.yaml 这类实际生效文件。

改完后执行一次重新部署（Deploy）即可生效。  
如果你要，我可以直接帮你把这三处改好。
