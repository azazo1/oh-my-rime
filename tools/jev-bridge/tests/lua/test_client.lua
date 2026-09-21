-- 传输层与缓存键的测试 (不依赖 librime, 直接用文件系统模拟 sidecar)

local function read_file(path)
    local handle = io.open(path, 'r')
    if not handle then return nil end
    local text = handle:read('a')
    handle:close()
    return text
end

local function write_file(path, text)
    local handle = assert(io.open(path, 'w'))
    handle:write(text)
    handle:close()
end

test('set_runtime_dir 覆盖队列与缓存目录并支持 ~ 与结尾斜杠', function()
    local home = CLIENT.home_dir()

    assert_true(CLIENT.set_runtime_dir(home .. '/.cache/rime-jev'))
    assert_eq(home .. '/.cache/rime-jev/queue', CLIENT.queue_dir)
    assert_eq(home .. '/.cache/rime-jev/cache', CLIENT.cache_dir)
    assert_eq(home .. '/.cache/rime-jev/log', CLIENT.log_dir)

    assert_true(CLIENT.set_runtime_dir('~/x/'))
    assert_eq(home .. '/x/queue', CLIENT.queue_dir)
    assert_true(CLIENT.set_runtime_dir('C:\\tmp\\rime-jev'))
    assert_eq('C:/tmp/rime-jev/cache', CLIENT.cache_dir)

    assert_true(not CLIENT.set_runtime_dir(''))
    assert_true(not CLIENT.set_runtime_dir(nil))

    -- 还原到测试沙箱: 后面的用例全靠它
    assert_true(CLIENT.set_runtime_dir(RUNTIME_DIR))
    assert_eq(RUNTIME_DIR .. '/queue', CLIENT.queue_dir)
end)

test('dir_command 按平台给出正确的建目录命令', function()
    local original = CLIENT.is_windows
    CLIENT.is_windows = false
    assert_eq('mkdir -p "/tmp/rime-jev"', CLIENT.dir_command('/tmp/rime-jev'))
    CLIENT.is_windows = true
    assert_eq(
        'if not exist "C:\\tmp\\rime-jev" mkdir "C:\\tmp\\rime-jev"',
        CLIENT.dir_command('C:/tmp/rime-jev')
    )
    CLIENT.is_windows = original
end)

test('fnv1a64 标准向量', function()
    assert_eq('cbf29ce484222325', CLIENT.fnv1a64_hex(''))
    assert_eq('af63dc4c8601ec8c', CLIENT.fnv1a64_hex('a'))
end)

test('缓存键与 Python 侧共享向量一致', function()
    local text = read_file(FIXTURE_PATH)
    assert_true(text ~= nil, '向量文件缺失: ' .. FIXTURE_PATH)
    local vectors = JSON.decode(text)
    assert_true(#vectors > 0, '向量为空')
    for _, vector in ipairs(vectors) do
        local key = CLIENT.cache_key(
            vector.schema_id,
            vector.code,
            vector.context,
            vector.candidates,
            vector.prompt_version
        )
        assert_eq(vector.cache_key, key, vector.name)
    end
end)

test('归一化规则', function()
    assert_eq('nihao', CLIENT.normalize_code(' Ni Hao\t'))
    assert_eq('a b', CLIENT.normalize_text('a\nb'))
    assert_eq('天气不错', CLIENT.tail_chars('今天天气不错', 4))
    assert_eq('', CLIENT.tail_chars('abc', 0))
    assert_eq('abc', CLIENT.tail_chars('abc', 10))
    assert_eq('后文', CLIENT.normalize_context('前文\n后文\t', 4))
    assert_eq('你\31好', CLIENT.join_candidates({ '你', '好' }))
end)

test('submit 写出的请求文件可以被解析', function()
    CLIENT.ensure_dirs()
    local request = { v = 1, id = 'unit' .. tostring(os.time()), code = 'nihao', ts = os.time() }
    local id = CLIENT.submit(request)
    assert_eq(request.id, id)
    local text = read_file(CLIENT.queue_dir .. '/' .. id .. '.req.json')
    assert_true(text ~= nil, '请求文件不存在')
    local decoded = JSON.decode(text)
    assert_eq('nihao', decoded.code)
    os.remove(CLIENT.queue_dir .. '/' .. id .. '.req.json')
end)

test('wait 读到响应后清理文件', function()
    CLIENT.ensure_dirs()
    local request = { v = 1, id = 'wait' .. tostring(os.time()), ts = os.time() }
    CLIENT.submit(request)
    local response_path = CLIENT.queue_dir .. '/' .. request.id .. '.res.json'
    write_file(response_path, JSON.encode({ v = 1, ok = true, order = JSON.array({ 1, 0 }) }))
    local response = CLIENT.wait(request.id, 500)
    assert_true(response ~= nil, '没有读到响应')
    assert_eq(true, response.ok)
    assert_eq(1, response.order[1])
    assert_true(read_file(response_path) == nil, '响应文件未清理')
    assert_true(read_file(CLIENT.queue_dir .. '/' .. request.id .. '.req.json') == nil, '请求文件未清理')
end)

test('wait 超时返回 nil 且保留请求文件', function()
    CLIENT.ensure_dirs()
    local request = { v = 1, id = 'timeout' .. tostring(os.time()), ts = os.time() }
    CLIENT.submit(request)
    local response, err = CLIENT.wait(request.id, 20)
    assert_nil(response, 'response')
    assert_eq('timeout', err)
    assert_true(read_file(CLIENT.queue_dir .. '/' .. request.id .. '.req.json') ~= nil, '请求文件应保留')
    os.remove(CLIENT.queue_dir .. '/' .. request.id .. '.req.json')
end)

test('read_cache 命中与过期', function()
    CLIENT.ensure_dirs()
    write_file(
        CLIENT.cache_dir .. '/fresh.json',
        JSON.encode({ v = 1, ok = true, order = JSON.array({ 0 }), expires_at = os.time() + 600 })
    )
    local payload = CLIENT.read_cache('fresh')
    assert_true(payload ~= nil, '未命中未过期缓存')
    assert_eq(0, payload.order[1])

    write_file(
        CLIENT.cache_dir .. '/stale.json',
        JSON.encode({ v = 1, order = JSON.array({ 0 }), expires_at = os.time() - 10 })
    )
    local expired, reason = CLIENT.read_cache('stale')
    assert_nil(expired, 'expired')
    assert_eq('expired', reason)
    assert_true(read_file(CLIENT.cache_dir .. '/stale.json') == nil, '过期缓存应删除')
end)

test('sidecar_alive 依据心跳文件判断', function()
    CLIENT.ensure_dirs()
    os.remove(CLIENT.queue_dir .. '/heartbeat')
    assert_true(not CLIENT.sidecar_alive(), '没有心跳应当视为离线')

    write_file(CLIENT.queue_dir .. '/heartbeat', tostring(os.time()))
    assert_true(CLIENT.sidecar_alive(), '新鲜心跳应当视为在线')

    write_file(CLIENT.queue_dir .. '/heartbeat', tostring(os.time() - 60))
    assert_true(not CLIENT.sidecar_alive(), '过期心跳应当视为离线')

    write_file(CLIENT.queue_dir .. '/heartbeat', '不是数字')
    assert_true(not CLIENT.sidecar_alive(), '心跳内容非法应当视为离线')
    os.remove(CLIENT.queue_dir .. '/heartbeat')
end)

test('fetch 在 sidecar 离线时不投递', function()
    CLIENT.ensure_dirs()
    os.remove(CLIENT.queue_dir .. '/heartbeat')
    local request = { v = 1, id = 'offline' .. tostring(os.time()), ts = os.time() }
    local payload, info = CLIENT.fetch(request, {
        key = 'offline-key',
        mode = 'sync',
        timeout_ms = 10,
        submit = true,
        alive = CLIENT.sidecar_alive(),
    })
    assert_nil(payload, 'payload')
    assert_eq('sidecar_down', info.reason)
    assert_true(read_file(CLIENT.queue_dir .. '/' .. request.id .. '.req.json') == nil, '不应投递')
end)

test('fetch 在 async 模式投递后立即返回', function()
    CLIENT.ensure_dirs()
    local request = { v = 1, id = 'async' .. tostring(os.time()), ts = os.time() }
    local payload, info = CLIENT.fetch(request, {
        key = 'not-cached-key',
        mode = 'async',
        timeout_ms = 10,
        submit = true,
    })
    assert_nil(payload, 'payload')
    assert_eq('pending', info.reason)
    assert_eq(request.id, info.id)
    os.remove(CLIENT.queue_dir .. '/' .. request.id .. '.req.json')
end)

test('fetch 命中缓存时不投递', function()
    CLIENT.ensure_dirs()
    write_file(
        CLIENT.cache_dir .. '/hitkey.json',
        JSON.encode({ v = 1, ok = true, order = JSON.array({ 1, 0 }), expires_at = os.time() + 60 })
    )
    local request = { v = 1, id = 'cache' .. tostring(os.time()), ts = os.time() }
    local payload, info = CLIENT.fetch(request, { key = 'hitkey', mode = 'sync', submit = true })
    assert_true(payload ~= nil, '应命中缓存')
    assert_eq(true, info.cached)
    assert_true(read_file(CLIENT.queue_dir .. '/' .. request.id .. '.req.json') == nil, '不应投递')
end)
