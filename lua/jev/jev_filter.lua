-- jev/jev_filter.lua
-- Rime 过滤器: 把候选交给 jev-bridge 打分后重排.
--
-- 注册方式 (wanxiang.custom.yaml):
--   "engine/filters/+":
--     - lua_filter@*jev.jev_filter
--
-- 设计原则: 这里是按键路径, 任何失败都只能原序放行, 不能抛错, 也不能长时间阻塞.
--   - async (默认): 只读缓存, 未命中就投递一个预取请求然后立刻返回;
--   - sync: 未命中时投递并等待 timeout_ms, 超时同样原序放行, 结果稍后进缓存;
--   - 配置来自 schema 的 jev_rerank: 段, 缺失时用 lua/jev/defaults.lua 的默认值.

local DEFAULTS = require('jev/defaults')
local CLIENT = require('jev/jev_client')
local RERANK = require('jev/jev_rerank')

local M = {}

-- ---------------------------------------------------------------- 配置读取

local function read_config(schema_config)
    local config = {}
    for key, value in pairs(DEFAULTS) do
        config[key] = value
    end
    if not schema_config then
        return config
    end

    local function get_string(path)
        local ok, value = pcall(function()
            return schema_config:get_string('jev_rerank/' .. path)
        end)
        if ok and type(value) == 'string' and value ~= '' then
            return value
        end
        return nil
    end

    local function get_number(path)
        local ok, value = pcall(function()
            return schema_config:get_int('jev_rerank/' .. path)
        end)
        if ok and type(value) == 'number' then
            return value
        end
        return nil
    end

    local function get_bool(path)
        local ok, value = pcall(function()
            return schema_config:get_bool('jev_rerank/' .. path)
        end)
        if ok and type(value) == 'boolean' then
            return value
        end
        return nil
    end

    local mode = get_string('mode')
    if mode == 'sync' or mode == 'async' then
        config.mode = mode
    end

    config.timeout_ms = get_number('timeout_ms') or config.timeout_ms
    config.max_candidates = get_number('max_candidates') or config.max_candidates
    config.min_code_len = get_number('min_code_len') or config.min_code_len
    config.context_chars = get_number('context_chars') or config.context_chars
    config.context_buffer_chars = get_number('context_buffer_chars')
        or config.context_buffer_chars
    config.prefetch_debounce_ms = get_number('prefetch_debounce_ms')
        or config.prefetch_debounce_ms

    local prefetch = get_bool('prefetch')
    if prefetch ~= nil then config.prefetch = prefetch end
    local show_confidence = get_bool('show_confidence')
    if show_confidence ~= nil then config.show_confidence = show_confidence end
    local debug = get_bool('debug')
    if debug ~= nil then config.debug = debug end

    config.badge = get_string('badge') or config.badge
    config.instructions = get_string('instructions') or config.instructions
    -- 换平台 (Linux/Windows) 时用它覆盖默认的 macOS 缓存目录, 必须与 sidecar config.toml 对齐
    config.runtime_dir = get_string('runtime_dir') or config.runtime_dir

    local schemas = {}
    local ok, list = pcall(function()
        return schema_config:get_list('jev_rerank/schemas')
    end)
    if ok and list and list.size and list.size > 0 then
        for index = 0, list.size - 1 do
            local name = get_string('schemas/@' .. index)
            if name then schemas[name] = true end
        end
        if next(schemas) then
            config.schemas = schemas
        end
    end

    return config
end

-- ---------------------------------------------------------------- 调试日志

local function log_debug(env, fmt, ...)
    local state = env.jev
    if not state or not state.config.debug then return end
    local ok, message = pcall(string.format, fmt, ...)
    if not ok then return end
    local handle = io.open(CLIENT.log_dir .. '/lua.log', 'a')
    if not handle then return end
    handle:write(os.date('%Y-%m-%d %H:%M:%S '), message, '\n')
    handle:close()
end

-- ---------------------------------------------------------------- 上下文

local function on_commit(env, ctx)
    local text = ctx:get_commit_text() or ''
    if text == '' then return end
    local state = env.jev
    local buffer = (state.context or '') .. text
    state.context = CLIENT.tail_chars(buffer, state.config.context_buffer_chars)
end

-- ---------------------------------------------------------------- 主体

local function should_submit(state, config)
    if config.mode == 'sync' then
        return true
    end
    if not config.prefetch then
        return false
    end
    local now = CLIENT.now_ms()
    local last = state.last_submit_ms
    if last and (now - last) < config.prefetch_debounce_ms then
        return false
    end
    state.last_submit_ms = now
    return true
end

local function eligible(state, env, candidates)
    local config = state.config
    local context = env.engine.context

    if not context:get_option('jev_rerank') then
        return false, 'switch_off'
    end
    if context:get_option('ascii_mode') then
        return false, 'ascii_mode'
    end

    local schema_id = (env.engine.schema and env.engine.schema.schema_id) or ''
    if not config.schemas[schema_id] then
        return false, 'schema_skip'
    end

    local code = context.input or ''
    if #code < config.min_code_len then
        return false, 'short_code'
    end
    if #candidates < 2 then
        return false, 'few_candidates'
    end

    local composition = context.composition
    local segment = composition and composition:back()
    if segment and not segment:has_tag('abc') then
        return false, 'tag_skip'
    end

    return true
end

local function rerank(candidates, env)
    local state = env.jev
    local config = state.config

    local allowed, reason = eligible(state, env, candidates)
    if not allowed then
        log_debug(env, '跳过 (%s)', tostring(reason))
        return
    end

    local picked, positions = RERANK.collect(candidates, config.max_candidates)
    if #picked < 2 then
        log_debug(env, '有效候选不足 (%d)', #picked)
        return
    end

    -- 协议里的 mode 只有 sync (会等待) 与 prefetch (不等), 与配置里的 async/sync 不是同一套取值
    local request_mode = (config.mode == 'sync') and 'sync' or 'prefetch'
    local request, key = RERANK.build_request({
        picked = picked,
        context = state.context,
        schema_id = env.engine.schema.schema_id,
        code = env.engine.context.input,
        mode = request_mode,
        timeout_ms = config.timeout_ms,
        context_chars = config.context_chars,
        prompt_version = config.prompt_version,
        instructions = config.instructions,
        badge = config.badge,
        show_confidence = config.show_confidence,
    })

    local submit = should_submit(state, config)
    local response, info = CLIENT.fetch(request, {
        key = key,
        mode = config.mode,
        timeout_ms = config.timeout_ms,
        submit = submit,
        alive = CLIENT.sidecar_alive(),
    })

    if not response then
        log_debug(env, '未命中 (%s) submit=%s key=%s', tostring(info.reason), tostring(submit), key)
        return
    end
    if response.ok ~= true then
        log_debug(env, '后端返回错误: %s', tostring(response.error))
        return
    end

    if RERANK.valid_order(response.order, #picked) then
        RERANK.apply_order(candidates, positions, response.order)
        log_debug(
            env,
            '重排完成 cached=%s 置信=%s 顺序=%s',
            tostring(info.cached),
            tostring(response.confidence),
            table.concat(response.order, ',')
        )
    else
        log_debug(env, '顺序无效, 保持原样')
    end
    RERANK.apply_badge(candidates, response.badge or config.badge)
end

function M.init(env)
    -- init 在方案部署时就会被调用, 一旦抛错会连累整个方案, 所以整体兜住异常.
    local ok, err = pcall(function()
        local schema_config = env.engine and env.engine.schema and env.engine.schema.config
        local config = read_config(schema_config)
        env.jev = {
            config = config,
            context = '',
            last_submit_ms = nil,
        }
        CLIENT.set_runtime_dir(config.runtime_dir)
        CLIENT.ensure_dirs()
        env.jev.commit_conn = env.engine.context.commit_notifier:connect(function(ctx)
            local committed, commit_error = pcall(on_commit, env, ctx)
            if not committed then
                log_debug(env, '上文缓冲失败: %s', tostring(commit_error))
            end
        end)
    end)
    if not ok then
        env.jev = nil
        io.stderr:write('jev_filter: 初始化失败, 已停用 AI 重排: ' .. tostring(err) .. '\n')
    end
end

function M.fini(env)
    if env.jev and env.jev.commit_conn then
        env.jev.commit_conn:disconnect()
    end
    env.jev = nil
end

-- ---------------------------------------------------------------- 候选遍历

--- 收集候选.
-- librime 传给过滤器的 input 是带 iter() 的对象, 万象所有过滤器都写作 `for cand in input:iter() do`;
-- 早期版本这里误写成 `for cand in input do`, 会让整条过滤器链抛异常导致候选区空白,
-- 因此这里既固定走真实 API, 也保留函数迭代器与数组的兜底形态.
local function collect_candidates(input)
    local candidates = {}
    local ok = pcall(function()
        if type(input) == 'function' then
            for candidate in input do
                candidates[#candidates + 1] = candidate
            end
        elseif input.iter ~= nil then
            for candidate in input:iter() do
                candidates[#candidates + 1] = candidate
            end
        else
            for _, candidate in ipairs(input) do
                candidates[#candidates + 1] = candidate
            end
        end
    end)
    return candidates, ok
end

function M.func(input, env)
    local candidates, collected = collect_candidates(input)
    if not collected then
        -- 取不到候选就不要再动顺序, 把已经拿到的部分原样放行
        if env.jev then
            log_debug(env, '候选遍历失败, 已按原序放行 (%d 个)', #candidates)
        end
        for index = 1, #candidates do
            yield(candidates[index])
        end
        return
    end

    if env.jev then
        local ok, err = pcall(rerank, candidates, env)
        if not ok then
            log_debug(env, '重排异常, 已按原序放行: %s', tostring(err))
        end
    end

    for index = 1, #candidates do
        yield(candidates[index])
    end
end

return M
