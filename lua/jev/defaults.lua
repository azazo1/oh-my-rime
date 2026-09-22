-- jev/defaults.lua
-- 默认配置与跨语言常量.
-- 这里出现的数值必须与 tools/jev-bridge/src/jev_bridge/{keys,config}.py 保持一致,
-- 否则缓存键或门限会在两侧分叉 (sidecar 会在日志里报 "缓存键不一致").

local PLATFORM = require('jev/jev_platform')

local M = {}

-- 协议版本: 缓存键前缀与请求体 v 字段共用
M.protocol_version = 1
M.prompt_version = 3

-- 运行时数据目录 (仓库之外, 不进版本控制).
-- 默认按平台推导, 规则与 sidecar 的 paths.py 一致; 也可以在 schema 的
-- jev_rerank/runtime_dir 里显式覆盖 (patch-config 会自动把本机路径写进去).
M.runtime_dir = PLATFORM.runtime_dir()
M.queue_dir = M.runtime_dir .. '/queue'
M.cache_dir = M.runtime_dir .. '/cache'

-- 行为开关
M.mode = 'async'              -- async (从不等待) | sync (有界等待)
M.timeout_ms = 30             -- sync 模式下单次重排的等待预算
M.prefetch = true             -- 上下文更新时提前投递请求
M.prefetch_debounce_ms = 80   -- 预取去抖, 避免每个按键都投递
M.max_candidates = 8          -- 参与重排的候选个数上限
M.min_code_len = 2            -- 编码长度低于它就跳过
M.min_context_chars = 3       -- 上文短于它就跳过 (没有依据时不要改词库顺序)
M.pinyin_scheme = 'flypy'     -- 双拼键位表 (目前支持 flypy = 小鹤双拼), 'none' 表示不展开
M.context_chars = 30          -- 送给模型的上文尾部字符数 (与缓存键共用)
M.context_buffer_chars = 120  -- Lua 侧保留的上文长度
-- 置信度/首选概率门限在 sidecar 侧 (config.toml 的 min_confidence / min_top_prob), 这里不重复定义
M.badge = 'AI'                -- 首位候选注释里追加的标记
M.show_confidence = false     -- 是否在标记里带上置信度百分比
M.schemas = { wanxiang = true, wanxiang_pro = true }
M.debug = false

-- 问题模板: 面向英文主训练的判定模型, 说明用英文, 状态里带中文.
-- "code 只是弱线索" 这句是必须的: 双拼方案下 code 是按键串 (例如 nihc 其实是 nihao), 模型认不出来,
-- 不说明的话它会去挑"拼音看起来最像 code"的候选, 反而把词库正确的首选挤下去.
M.instructions = table.concat({
  'The user is typing Chinese, and the text before the cursor is given as context.',
  'Choose which candidate the user most likely intends next.',
  'Prefer the candidate that reads naturally after the context',
  'and that accounts for the whole typed code rather than only its beginning;',
  'the code is a keyboard string and pinyin_hint is a best-effort expansion of it,',
  'so neither of them is reliable evidence on its own.',
}, ' ')

return M
