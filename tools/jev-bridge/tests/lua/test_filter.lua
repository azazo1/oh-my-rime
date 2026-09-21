-- jev_filter 的离线测试: 用假 env 模拟 librime, 验证过滤器的跳过条件, 重排与标记.
-- 这样不必真的部署到 Squirrel 就能覆盖过滤器的接口形状与失败兜底.

local FILTER = require('jev/jev_filter')
local DEFAULTS = require('jev/defaults')

local function make_candidate(text, kind, comment)
    return { text = text, type = kind or 'sentence', comment = comment or '' }
end

local function make_config(overrides)
    local values = overrides or {}
    -- runtime_dir 必须存在且指向测试沙箱: 过滤器 init 会用它覆盖 CLIENT 的目录
    if values.runtime_dir == nil then
        values.runtime_dir = RUNTIME_DIR
    end
    return {
        get_string = function(_, path)
            local key = path:gsub('^jev_rerank/', '')
            -- 模拟 librime 的 "列表用 /@i 逐项取字符串" 写法
            local list_index = key:match('^schemas/@(%d+)$')
            if list_index and values.schemas then
                return values.schemas[tonumber(list_index) + 1]
            end
            return values[key]
        end,
        get_int = function(_, path)
            local key = path:gsub('^jev_rerank/', '')
            local value = values[key]
            return type(value) == 'number' and value or nil
        end,
        get_bool = function(_, path)
            local key = path:gsub('^jev_rerank/', '')
            local value = values[key]
            return type(value) == 'boolean' and value or nil
        end,
        get_list = function(_, path)
            if values.schemas and path == 'jev_rerank/schemas' then
                return { size = #values.schemas }
            end
            return nil
        end,
    }
end

local function make_env(options)
    local opts = options or {}
    local state = { connected = false, disconnected = false }
    local context = {
        input = opts.input or 'nihao',
        get_option = function(_, name)
            if name == 'jev_rerank' then return opts.switch ~= false end
            if name == 'ascii_mode' then return opts.ascii == true end
            return false
        end,
        composition = {
            back = function()
                return {
                    has_tag = function(_, tag)
                        return opts.tag == nil and tag == 'abc' or opts.tag == tag
                    end,
                }
            end,
        },
        commit_notifier = {
            connect = function(_, fn)
                state.connected = true
                state.commit_fn = fn
                return {
                    disconnect = function() state.disconnected = true end,
                }
            end,
        },
    }
    local env = {
        engine = {
            schema = {
                schema_id = opts.schema_id or 'wanxiang',
                config = make_config(opts.config_values),
            },
            context = context,
        },
    }
    return env, state
end

--- librime 传给过滤器的输入对象形状: 只有 iter() 方法.
-- 之前这里错用了函数迭代器, 导致真实环境里 `for cand in input do` 抛异常, 候选区直接空白.
local function make_input(candidates)
    return {
        iter = function()
            local index = 0
            return function()
                index = index + 1
                return candidates[index]
            end
        end,
    }
end

local function run_filter(env, candidates, input_override)
    local collected = {}
    local previous_yield = _G.yield
    _G.yield = function(candidate) collected[#collected + 1] = candidate end
    local ok, err = pcall(function()
        FILTER.func(input_override or make_input(candidates), env)
    end)
    _G.yield = previous_yield
    return collected, ok, err
end

local function texts(candidates)
    local out = {}
    for index, candidate in ipairs(candidates) do
        out[index] = candidate.text
    end
    return table.concat(out, ',')
end

local function write_cache(key, order, badge)
    local path = CLIENT.cache_dir .. '/' .. key .. '.json'
    local handle = assert(io.open(path, 'w'))
    handle:write(JSON.encode({
        v = 1,
        ok = true,
        order = JSON.array(order),
        badge = badge or 'AI',
        confidence = 0.9,
        expires_at = os.time() + 300,
    }))
    handle:close()
    return path
end

local function key_for(code, context, candidate_texts)    local texts_normalized = {}
    for index, text in ipairs(candidate_texts) do
        texts_normalized[index] = CLIENT.normalize_text(text)
    end
    return CLIENT.cache_key(
        'wanxiang',
        code,
        CLIENT.normalize_context(context, DEFAULTS.context_chars),
        texts_normalized,
        DEFAULTS.prompt_version
    )
end

--- 读取队列里最新一个请求的 mode 字段 (协议里只有 sync / prefetch 两种)
local function read_last_request_mode()
    local pipe = io.popen('ls -t "' .. CLIENT.queue_dir .. '"/*.req.json 2>/dev/null | head -1')
    local path = pipe:read('*l')
    pipe:close()
    if not path or path == '' then return nil end
    local handle = io.open(path, 'r')
    if not handle then return nil end
    local text = handle:read('a')
    handle:close()
    local decoded = JSON.decode(text)
    return decoded and decoded.mode
end

local function clear_queue()
    os.execute('rm -f "' .. CLIENT.queue_dir .. '"/*.req.json')
end

--- 写一个新鲜的心跳, 表示 sidecar 在线
local function touch_heartbeat(age_s)
    CLIENT.ensure_dirs()
    local handle = assert(io.open(CLIENT.queue_dir .. '/heartbeat', 'w'))
    handle:write(tostring(os.time() - (age_s or 0)))
    handle:close()
end

local function drop_heartbeat()
    os.remove(CLIENT.queue_dir .. '/heartbeat')
end

test('过滤器初始化与释放不抛异常', function()
    local env, state = make_env()
    assert_true(pcall(FILTER.init, env))
    assert_true(env.jev ~= nil, 'env.jev')
    assert_true(state.connected, 'commit_notifier 未连接')
    assert_true(pcall(FILTER.fini, env))
    assert_true(state.disconnected, '连接未断开')
end)

test('开关关闭时不改动候选', function()
    local env = make_env({ switch = false })
    FILTER.init(env)
    local candidates = { make_candidate('你好'), make_candidate('尼豪') }
    local collected = run_filter(env, candidates)
    assert_eq(2, #collected)
    assert_eq('你好,尼豪', texts(collected))
    assert_eq('', collected[1].comment)
end)

test('命中缓存时按顺序重排并追加标记', function()
    local context = '今天天气不错'
    local code = 'nihao'
    local names = { '你好', '尼豪', '拟好' }
    local key = key_for(code, context, names)
    write_cache(key, { 2, 0, 1 })

    local env = make_env({ input = code })
    FILTER.init(env)
    -- 模拟已经上屏的上文
    env.engine.context.commit_notifier.connect = function(_, fn)
        fn({ get_commit_text = function() return context end })
        return { disconnect = function() end }
    end
    FILTER.init(env)

    local candidates = { make_candidate(names[1]), make_candidate(names[2]), make_candidate(names[3]) }
    local collected = run_filter(env, candidates)
    assert_eq('拟好,你好,尼豪', texts(collected))
    assert_eq('AI', collected[1].comment)
end)

test('缓存里的顺序不合法时保持原顺序但仍加标记', function()
    local code = 'ceshi'
    local names = { '测试', '侧视' }
    local key = key_for(code, '', names)
    write_cache(key, { 5, 5 })

    local env = make_env({ input = code })
    FILTER.init(env)
    local candidates = { make_candidate(names[1]), make_candidate(names[2]) }
    local collected = run_filter(env, candidates)
    assert_eq('测试,侧视', texts(collected))
    assert_eq('AI', collected[1].comment)
end)

test('编码过短或候选过少时跳过', function()
    local env = make_env({ input = 'n' })
    FILTER.init(env)
    local collected = run_filter(env, { make_candidate('你'), make_candidate('拟') })
    assert_eq('你,拟', texts(collected))
    assert_eq('', collected[1].comment)

    local env2 = make_env({ input = 'nihao' })
    FILTER.init(env2)
    local collected2 = run_filter(env2, { make_candidate('你') })
    assert_eq('你', texts(collected2))
    assert_eq('', collected2[1].comment)
end)

test('英文模式与非 abc 段落跳过', function()
    local env = make_env({ ascii = true })
    FILTER.init(env)
    local collected = run_filter(env, { make_candidate('你好'), make_candidate('尼豪') })
    assert_eq('', collected[1].comment)

    local env2 = make_env({ tag = 'punct' })
    FILTER.init(env2)
    local collected2 = run_filter(env2, { make_candidate('你好'), make_candidate('尼豪') })
    assert_eq('', collected2[1].comment)
end)

test('方案不在白名单时跳过', function()
    local env = make_env({ schema_id = 'wanxiang_reverse' })
    FILTER.init(env)
    local collected = run_filter(env, { make_candidate('你好'), make_candidate('尼豪') })
    assert_eq('', collected[1].comment)
end)

test('没有缓存且 async 模式时投递请求并原序返回', function()
    touch_heartbeat()
    local env = make_env({ input = 'qingshu' })
    FILTER.init(env)
    local candidates = { make_candidate('请示'), make_candidate('情书') }
    local collected = run_filter(env, candidates)
    assert_eq('请示,情书', texts(collected))
    assert_eq('', collected[1].comment)
    -- 队列里应当出现等待 sidecar 消费的请求文件
    local pipe = io.popen('ls -1 "' .. CLIENT.queue_dir .. '"/*.req.json 2>/dev/null | wc -l')
    local count = tonumber(pipe:read('*a'))
    pipe:close()
    assert_true(count >= 1, '未投递请求')
end)

test('sidecar 不在线时不投递也不等待', function()
    clear_queue()
    drop_heartbeat()
    local env = make_env({ input = 'meirenkuang' })
    FILTER.init(env)
    local started = CLIENT.now_ms()
    local collected = run_filter(env, { make_candidate('没人'), make_candidate('美人') })
    local elapsed = CLIENT.now_ms() - started
    assert_eq('没人,美人', texts(collected))
    assert_true(elapsed < 50, '不应等待, 实际 ' .. string.format('%.1f', elapsed) .. 'ms')
    local pipe = io.popen('ls -1 "' .. CLIENT.queue_dir .. '"/*.req.json 2>/dev/null | wc -l')
    local count = tonumber(pipe:read('*a'))
    pipe:close()
    assert_eq(0, count, '不应投递请求')
end)

test('请求里的 mode 取值符合协议 (async -> prefetch, sync -> sync)', function()
    -- 曾经把配置里的 async 直接塞进请求, sidecar 判为 bad_mode 全部丢弃, 表现是"看不到 AI 生效"
    clear_queue()
    touch_heartbeat()
    local env = make_env({ input = 'qingqiukuang' })
    FILTER.init(env)
    run_filter(env, { make_candidate('请求框'), make_candidate('情况况') })
    assert_eq('prefetch', read_last_request_mode())

    clear_queue()
    touch_heartbeat()
    local env2 = make_env({
        input = 'qingqiukuang',
        config_values = { mode = 'sync', timeout_ms = 5 },
    })
    FILTER.init(env2)
    run_filter(env2, { make_candidate('请求框'), make_candidate('情况况') })
    assert_eq('sync', read_last_request_mode())
end)

test('配置段可以覆盖默认值', function()
    local env = make_env({
        config_values = {
            mode = 'sync',
            timeout_ms = 5,
            max_candidates = 3,
            min_code_len = 4,
            badge = 'JEV',
            show_confidence = true,
            debug = true,
            schemas = { 'wanxiang' },
        },
    })
    FILTER.init(env)
    assert_eq('sync', env.jev.config.mode)
    assert_eq(5, env.jev.config.timeout_ms)
    assert_eq(3, env.jev.config.max_candidates)
    assert_eq(4, env.jev.config.min_code_len)
    assert_eq('JEV', env.jev.config.badge)
    assert_eq(true, env.jev.config.show_confidence)
    assert_eq(true, env.jev.config.debug)
    assert_eq(true, env.jev.config.schemas.wanxiang)
    assert_nil(env.jev.config.schemas.wanxiang_pro)
end)

test('init 未执行时过滤器原样放行', function()
    local env = make_env()
    local candidates = { make_candidate('你好'), make_candidate('尼豪') }
    local collected, ok = run_filter(env, candidates)
    assert_true(ok, '不应抛异常')
    assert_eq('你好,尼豪', texts(collected))
end)

test('函数迭代器形态的输入也能工作', function()
    local env = make_env()
    FILTER.init(env)
    local candidates = { make_candidate('你好'), make_candidate('尼豪') }
    local index = 0
    local collected, ok = run_filter(env, candidates, function()
        index = index + 1
        return candidates[index]
    end)
    assert_true(ok, '不应抛异常')
    assert_eq('你好,尼豪', texts(collected))
end)

test('输入对象完全不可迭代时不抛异常', function()
    local env = make_env()
    FILTER.init(env)
    local hostile = setmetatable({}, {
        __index = function() error('不应该被这样访问') end,
        __call = function() error('不应该被调用') end,
    })
    local collected, ok = run_filter(env, {}, hostile)
    assert_true(ok, '过滤器绝不能把异常抛回 librime')
    assert_eq(0, #collected)
end)
