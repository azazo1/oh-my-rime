-- 极简 JSON 编解码的测试

test('json 编码字符串与中文', function()
    assert_eq('"你好"', JSON.encode('你好'))
    assert_eq('"a\\"b"', JSON.encode('a"b'))
    assert_eq('"a\\nb"', JSON.encode('a\nb'))
    assert_eq('"a\\tb"', JSON.encode('a\tb'))
    assert_eq('"a\\\\b"', JSON.encode('a\\b'))
end)

test('json 编码数组与对象', function()
    assert_eq('[1,2]', JSON.encode(JSON.array({ 1, 2 })))
    assert_eq('[]', JSON.encode(JSON.array({})))
    assert_eq('{"a":1,"b":"x"}', JSON.encode({ b = 'x', a = 1 }))
    assert_eq('true', JSON.encode(true))
    assert_eq('null', JSON.encode(nil))
end)

test('json 往返一致', function()
    local payload = {
        v = 1,
        ok = true,
        order = JSON.array({ 2, 0, 1 }),
        scores = { ['0'] = 0.1, ['1'] = 0.2, ['2'] = 0.7 },
        badge = 'AI 70%',
        note = '含 "引号" 与换行\n',
    }
    local text = JSON.encode(payload)
    local decoded = JSON.decode(text)
    assert_eq(2, decoded.order[1])
    assert_eq(0.7, decoded.scores['2'])
    assert_eq('AI 70%', decoded.badge)
    assert_eq(payload.note, decoded.note)
end)

test('json 解码转义与 unicode', function()
    local decoded = JSON.decode('{"a":"\\u4f60\\u597d"}')
    assert_eq('你好', decoded.a)
    -- 代理对: U+1F600
    local emoji = JSON.decode('{"a":"\\ud83d\\ude00"}')
    assert_eq(utf8.char(0x1F600), emoji.a)
end)

test('json 解码 sidecar 响应', function()
    local text = table.concat({
        '{"v":1,"id":"abc","ok":true,"cached":false,"backend":"mock","model":"mock-1",',
        '"latency_ms":0.0,"expires_at":1790000000,"order":[1,0],"scores":{"0":0.05,"1":0.95},',
        '"confidence":0.9,"order_changed":true,"badge":"AI","cache_key":"k","error":null}',
    })
    local decoded = JSON.decode(text)
    assert_eq(1, decoded.v)
    assert_eq(true, decoded.ok)
    assert_eq(false, decoded.cached)
    assert_eq(1, decoded.order[1])
    assert_eq(0.95, decoded.scores['1'])
    assert_eq('AI', decoded.badge)
    assert_nil(decoded.error, 'error')
end)

test('json 解码失败返回错误而不是抛异常', function()
    local value, err = JSON.decode('{ 不是 JSON')
    assert_nil(value, 'value')
    assert_true(err ~= nil, 'err')
    assert_nil(JSON.decode(''))
    assert_nil(JSON.decode('{"a":}'))
    assert_nil(JSON.decode('[1,2'))
    assert_nil(JSON.decode('{"a":1} 尾巴'))
end)

test('json 解码数字形态', function()
    local decoded = JSON.decode('[0, -1, 3.5, 1e3]')
    assert_eq(0, decoded[1])
    assert_eq(-1, decoded[2])
    assert_eq(3.5, decoded[3])
    assert_eq(1000, decoded[4])
end)
