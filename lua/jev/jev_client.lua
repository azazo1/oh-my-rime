-- jev/jev_client.lua
-- 与 sidecar 的传输层: 文件队列投递 + 有界等待 + 本地缓存直读.
-- librime-lua 只内建 utf8, 没有 socket, 所以这里完全不依赖第三方 Lua 模块.
--
-- 契约 (必须与 tools/jev-bridge/src/jev_bridge/keys.py 一致):
--   队列: <queue_dir>/<id>.req.json  与  <id>.res.json (先写 tmp 再 rename)
--   缓存: <cache_dir>/<key>.json, key = FNV-1a64(键串), 键串见 M.cache_key

local JSON = require('jev/jev_json')
local DEFAULTS = require('jev/defaults')

local M = {}

M.queue_dir = DEFAULTS.queue_dir
M.cache_dir = DEFAULTS.cache_dir
M.log_dir = DEFAULTS.runtime_dir .. '/log'

local FNV_OFFSET = 0xcbf29ce484222325
local FNV_PRIME = 0x100000001b3
local CANDIDATE_SEPARATOR = string.char(31)
local KEY_PREFIX = 'v1'

local id_counter = 0
local dirs_ready = false

-- ---------------------------------------------------------------- 基础工具

--- 毫秒时钟, 仅用于测量时间间隔 (rime_api 提供的时钟不保证是 epoch)
function M.now_ms()
    if rime_api and rime_api.get_time_ms then
        return rime_api.get_time_ms()
    end
    return os.clock() * 1000
end

function M.fnv1a64_hex(text)
    local hash = FNV_OFFSET
    for index = 1, #text do
        hash = hash ~ text:byte(index)
        hash = hash * FNV_PRIME
    end
    return string.format('%016x', hash)
end

--- 取末尾 limit 个码点. limit 为 nil 表示调用方已经截断过, 原样返回.
function M.tail_chars(text, limit)
    if not text or text == '' then return '' end
    if limit == nil then return text end
    if limit <= 0 then return '' end
    local count = utf8.len(text)
    if not count then return text end
    if count <= limit then return text end
    local start = utf8.offset(text, count - limit + 1)
    if not start then return text end
    return text:sub(start)
end

function M.normalize_text(text)
    local result = (text or ''):gsub('[\r\n\t]', ' ')
    return result
end

function M.normalize_code(code)
    local result = (code or ''):gsub('[ \t\r\n\f\v]', '')
    return result:lower()
end

function M.normalize_context(context, limit)
    local text = M.normalize_text(context)
    text = M.tail_chars(text, limit)
    local result = text:gsub('^ +', ''):gsub(' +$', '')
    return result
end

function M.join_candidates(texts)
    local parts = {}
    for index = 1, #texts do
        parts[index] = M.normalize_text(texts[index])
    end
    return table.concat(parts, CANDIDATE_SEPARATOR)
end

--- 返回 (缓存键, 参与哈希的键串). context 视为已按 context_chars 截断.
function M.cache_key(schema_id, code, context, texts, prompt_version)
    local source = table.concat({
        KEY_PREFIX,
        schema_id or '',
        M.normalize_code(code),
        M.normalize_context(context),
        tostring(prompt_version or DEFAULTS.prompt_version),
        M.join_candidates(texts or {}),
    }, '|')
    return M.fnv1a64_hex(source), source
end

function M.new_id()
    id_counter = id_counter + 1
    return string.format(
        '%x%06x%04x',
        os.time(),
        id_counter % 0x1000000,
        math.random(0, 0xffff)
    )
end

-- ---------------------------------------------------------------- 目录与队列

--- 建立运行时目录. 即使失败也只尝试一次, 避免每次按键都 fork 一个 shell.
function M.ensure_dirs()
    if dirs_ready then return true end
    dirs_ready = true
    local command = string.format(
        'mkdir -p "%s" "%s" "%s" 2>/dev/null',
        M.queue_dir,
        M.cache_dir,
        M.log_dir
    )
    local ok = os.execute(command)
    if ok == nil or ok == false then
        return false
    end
    return true
end

function M.submit(request)
    M.ensure_dirs()
    local text, encode_error = JSON.encode(request)
    if not text then
        return nil, encode_error or 'encode_failed'
    end
    local path = M.queue_dir .. '/' .. request.id .. '.req.json'
    local temporary = path .. '.tmp'
    local handle, open_error = io.open(temporary, 'w')
    if not handle then
        return nil, open_error or 'open_failed'
    end
    handle:write(text)
    handle:close()
    if not os.rename(temporary, path) then
        os.remove(temporary)
        return nil, 'rename_failed'
    end
    return request.id
end

--- 有界等待响应. 超时返回 nil, 'timeout', 结果稍后会落进缓存.
function M.wait(id, budget_ms)
    local response_path = M.queue_dir .. '/' .. id .. '.res.json'
    local request_path = M.queue_dir .. '/' .. id .. '.req.json'
    local started = M.now_ms()
    local budget = budget_ms or DEFAULTS.timeout_ms
    while true do
        local handle = io.open(response_path, 'r')
        if handle then
            local text = handle:read('a')
            handle:close()
            os.remove(response_path)
            os.remove(request_path)
            local payload, decode_error = JSON.decode(text)
            if not payload then
                return nil, decode_error or 'decode_failed'
            end
            return payload
        end
        if M.now_ms() - started >= budget then
            return nil, 'timeout'
        end
    end
end

function M.read_cache(key)
    if not key or key == '' then return nil, 'no_key' end
    local path = M.cache_dir .. '/' .. key .. '.json'
    local handle = io.open(path, 'r')
    if not handle then
        return nil, 'miss'
    end
    local text = handle:read('a')
    handle:close()
    local payload, decode_error = JSON.decode(text)
    if not payload then
        return nil, decode_error or 'decode_failed'
    end
    if payload.expires_at and payload.expires_at <= os.time() then
        os.remove(path)
        return nil, 'expired'
    end
    return payload
end

--- sidecar 是否在线: 队列目录里的 heartbeat 文件是否足够新.
-- 不在线时不要投递 (否则请求文件无界堆积), sync 模式也不要空等.
function M.sidecar_alive(max_age_s)
    local handle = io.open(M.queue_dir .. '/heartbeat', 'r')
    if not handle then
        return false
    end
    local text = handle:read('a')
    handle:close()
    local stamp = tonumber(text)
    if not stamp then
        return false
    end
    return (os.time() - stamp) <= (max_age_s or 5)
end

--- 一次完整的取数: 先查缓存, 未命中且 sidecar 在线才投递 (sync 模式才等待).
function M.fetch(request, options)
    local payload, reason = M.read_cache(options.key)
    if payload then
        return payload, { cached = true, reason = 'cache' }
    end
    if not options.submit then
        return nil, { cached = false, reason = reason or 'miss' }
    end
    if options.alive == false then
        return nil, { cached = false, reason = 'sidecar_down' }
    end
    local id, submit_error = M.submit(request)
    if not id then
        return nil, { cached = false, reason = submit_error or 'submit_failed' }
    end
    if options.mode ~= 'sync' then
        return nil, { cached = false, reason = 'pending', id = id }
    end
    local response, wait_error = M.wait(id, options.timeout_ms)
    if not response then
        return nil, { cached = false, reason = wait_error or 'no_response', id = id }
    end
    return response, { cached = false, reason = 'fresh', id = id }
end

return M
