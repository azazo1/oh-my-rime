-- jev/defaults.lua
-- 默认配置与跨语言常量.
-- 这里出现的数值必须与 tools/jev-bridge/src/jev_bridge/{keys,config}.py 保持一致,
-- 否则缓存键或门限会在两侧分叉 (sidecar 会在日志里报 "缓存键不一致").

local HOME = os.getenv('HOME') or '/tmp'

local M = {}

-- 协议版本: 缓存键前缀与请求体 v 字段共用
M.protocol_version = 1
M.prompt_version = 1

-- 运行时数据目录 (仓库之外, 不进版本控制)
M.runtime_dir = HOME .. '/Library/Caches/rime-jev'
M.queue_dir = M.runtime_dir .. '/queue'
M.cache_dir = M.runtime_dir .. '/cache'

-- 行为开关
M.mode = 'async'              -- async (从不等待) | sync (有界等待)
M.timeout_ms = 30             -- sync 模式下单次重排的等待预算
M.prefetch = true             -- 上下文更新时提前投递请求
M.prefetch_debounce_ms = 80   -- 预取去抖, 避免每个按键都投递
M.max_candidates = 8          -- 参与重排的候选个数上限
M.min_code_len = 2            -- 编码长度低于它就跳过
M.context_chars = 30          -- 送给模型的上文尾部字符数 (与缓存键共用)
M.context_buffer_chars = 120  -- Lua 侧保留的上文长度
M.min_confidence = 0.5        -- 顺序变化的最小置信度
M.min_top_prob = 0.34         -- 首选概率下限
M.badge = 'AI'                -- 首位候选注释里追加的标记
M.show_confidence = false     -- 是否在标记里带上置信度百分比
M.schemas = { wanxiang = true, wanxiang_pro = true }
M.debug = false

-- 问题模板: 面向英文主训练的判定模型, 说明用英文, 状态里带中文
M.instructions = table.concat({
  'The user is typing Chinese with a pinyin input method.',
  'Given the code being typed and the text typed just before it,',
  'choose which candidate the user most likely intends.',
  'Prefer the candidate that reads naturally after the context.',
}, ' ')

return M
