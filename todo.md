# Rime 万象拼音的 AI 候选能力 - 设计待办

本文件只记录设计, 不作为实现承诺. 已落地的第一部分 (最小链路: sidecar + 缓存重排) 见
`tools/jev-bridge/README.md` 与 `lua/jev/`.

## 0. 现状 (已实现)

- `lua/jev/jev_filter.lua` 作为 `lua_filter` 追加在万象全部过滤器之后, 只改顺序与首位注释.
- `tools/jev-bridge` 提供文件队列, 缓存, 后端转接 (`mock` / Jev 兼容的 `http`), 以及调试用 HTTP 端点.
- 后端可选: 云端 Jev (`api.typesafe.ai`, 70-500ms, 英文域内最好), 本地 Laya/MLX
  (`localjev-mlx` 或 `laya-mlx`, 多语言 322M 权重, M3 Max 上 P50 7-14ms, M1 需实测).
- 已知边界: Jev/Laya 不做文本生成, 只能对**已有候选**做判断与排序, 因此 "AI 造词" 不在能力范围内;
  官方也说明 CJK 准确率低于英文, 中文重排的实际收益必须用离线基准量化, 不能凭感觉开启.

## 1. 整句 / 长句 N-best 优选

- 目标: 打完一串拼音后, 从 `script_translator` + `librime-octagram` 给出的整句候选里挑出最通顺的一条.
- 触发时机: 码长 >= 6, 或上屏前最后一次刷新; 句级重排对延迟不敏感, 可以只用 `async`.
- 问题设计: 与现在同一套 `choice`, 但 `instructions` 换成 "整句是否与上文衔接自然", criteria 用整句文本.
- 需要先做的事: 用 `super_comment` 的 `cand_type.sentence` 标记识别 sentence 候选 (现在只按 `type ~= punct` 过滤);
  评估 `grammar/collocation_penalty` 与模型判断冲突时谁优先.
- 风险: 整句候选数量大, 送进模型的 state 会变长, 需要按字符预算裁剪候选文本.

## 2. 联想 / 预测候选的重排

- 现在 `lua/wanxiang/user_predict.lua` 自己养 `predict.userdb`, `librime-predict` 插件也能给候选.
- 方案: 对 `predict` / `completion` 类型的候选单独做一次 `choice`, 与上文的衔接关系比词频更可靠.
- 需要先做的事: 确认与 `user_predict*F` 的执行顺序 (目前 Jev 过滤器在它之后, 会覆盖它的调频结果),
  或者在请求里带上 `user_predict` 给出的分数作为参考, 让模型只在词语明显不合语境时下移.
- 开关建议: 预测重排与候选重排分开两个 switch, 便于单独评估收益.

## 3. 中英混输与 `wanxiang_english`

- Jev/Laya 的英文能力最强, 这一块是收益最稳的场景: 英文单词候选排序, 中英混输时的分词与空格策略,
  `super_english` 的语句流决策.
- 需要先做的事: 用英文语料做一个可比基准 (首选率), 再决定是否默认开启.
- 注意: `wanxiang_english` 目前不在 `default.yaml` 的 `schema_list` 里, 评估时需要临时切换方案.

## 4. 离线词库整理 (批处理, 不影响打字延迟)

- 数据来源: `wanxiang.userdb` / `lua/predict.userdb` 里的用户词条, 以及 `input_statistics.lua` 记录的历史输入.
- 做法: 导出词条 -> 用 `noul` ("这条记录像不像误触选错造成的") 与 `score` (词频合理性) 批量打分 ->
  产出 (a) 清洗黑名单, (b) 权重修正表, (c) 建议加入 `custom_phrase.txt` 的短语.
- 落地形态: 独立的 uv 脚本 (`tools/jev-bridge` 里加子命令或单独模块), 结果先出报告再由人确认,
  绝不自动改写 userdb.
- 需要先做的事: 确认 leveldb 的读取方式 (rime 自带的 `rime_dict_manager --export` 或 Python leveldb 绑定),
  并把"人工确认"做成显式步骤.

## 5. 性能与精度优化

- LuaSocket 快路径: 若允许装 `luarocks`/`luasocket`, 可以把"文件投递 + 有界等待"换成毫秒级本地 socket.
  当前不装, 是因为多一个 C 模块就多一处随 Squirrel 升级失效的风险.
- 进程内后端: `pip install laya-mlx` 后由 sidecar 直接调用 (少一跳 HTTP), 并打开 `compile=True`,
  `pad_to_multiple=16`, `cache_prompts=True`; 需要用 `just bench` 对比是否真的更快.
- 请求合并: 现在同键请求已合并; 还可以把 "同一上文的相邻编码" 合并成一次多问题调用
  (官方文档说明同一 state 下增加问题几乎不增加延迟).
- 分级路由: 本地 Laya 做快速初筛, 置信度低时再升级到云端 Jev 复核; 需要额外的置信度阈值标定.
- 中文精度: 若 CJK 表现不达标, 备选是 "英文说明 + 罗马化编码" 的提示词变体, 或改用本地中文小模型
  (对比方案见 `zhanghaozhecn/rime-llm-rerank` 的跨熵打分思路).

## 6. 精度评估方法

- 从 `input_statistics.lua` 的历史里抽 N 条真实输入 (编码 + 最终上屏文本 + 上文) 做离线基准.
- 指标: 首选率 (目标词是否被排到第一位), 选重率, 顺序变化率, 以及"变化后反而变差"的比例.
- 每次改 `instructions` / 门限 / 后端都要重跑基准, 结果记进本文件或单独的评估文档.

## 7. 风险与开关

- 模型判断错误导致首位候选变化: 由 `min_confidence` + `min_top_prob` 双重门限兜底, 不达标就不改顺序.
- 手动排序冲突: `super_sequence*F` 的位移记录目前会被 Jev 过滤器覆盖; 改进方向是检测到位移记录时跳过重排.
- 隐私: 云端后端会把上文送出本机, 默认禁止; 如需使用必须显式开启, 并在文档中显著提示.
- 稳定性: sidecar 不在时, Lua 只做一次缓存查找, 不等待不报错; Squirrel 升级只需要官方包, 不替换二进制.
- Rime 用户目录同步: 运行时数据都在 `~/Library/Caches/rime-jev`, 不参与 Rime 的 sync.
