-- 重排纯逻辑的测试

local RERANK = require('jev/jev_rerank')
local DEFAULTS = require('jev/defaults')

local function make_candidates(texts, types)
    local candidates = {}
    for index, text in ipairs(texts) do
        candidates[index] = {
            text = text,
            comment = '',
            type = types and types[index] or 'sentence',
        }
    end
    return candidates
end

test('collect 跳过标点并记录原始位置', function()
    local candidates = make_candidates({ '你好', '，', '尼豪', '拟好' }, { 'sentence', 'punct', 'sentence', 'sentence' })
    local picked, positions = RERANK.collect(candidates, 8)
    assert_eq(3, #picked)
    assert_eq('你好', picked[1].text)
    assert_eq('尼豪', picked[2].text)
    assert_eq(1, positions[1])
    assert_eq(3, positions[2])
    assert_eq(4, positions[3])
end)

test('collect 尊重候选数量上限', function()
    local candidates = make_candidates({ 'a', 'b', 'c', 'd' })
    local picked = RERANK.collect(candidates, 2)
    assert_eq(2, #picked)
end)

test('build_request 结构与缓存键', function()
    local candidates = make_candidates({ '你好', '尼豪' })
    local picked, positions = RERANK.collect(candidates, 8)
    local request, key = RERANK.build_request({
        picked = picked,
        context = '今天天气不错',
        schema_id = 'wanxiang',
        code = 'nihao',
        mode = 'sync',
        context_chars = 4,
        prompt_version = DEFAULTS.prompt_version,
        instructions = 'pick one',
        badge = 'AI',
        show_confidence = false,
    })
    assert_eq(1, request.v)
    assert_eq('sync', request.mode)
    assert_eq('wanxiang', request.schema_id)
    assert_eq('choice', request.question.type)
    assert_eq('你好', request.question.criteria['0'])
    assert_eq('尼豪', request.question.criteria['1'])
    assert_eq(0, request.candidates[1].index)
    assert_eq(1, request.candidates[2].index)
    -- 上文按 context_chars 截断
    assert_eq('天气不错', request.context)
    assert_eq(key, request.cache_key)
    local expected = CLIENT.cache_key('wanxiang', 'nihao', '天气不错', { '你好', '尼豪' }, DEFAULTS.prompt_version)
    assert_eq(expected, key)
    assert_eq(2, #positions)
end)

test('valid_order 只接受完整排列', function()
    assert_true(RERANK.valid_order({ 1, 0 }, 2))
    assert_true(RERANK.valid_order({ 2, 0, 1 }, 3))
    assert_true(not RERANK.valid_order({ 0, 0 }, 2), '重复值')
    assert_true(not RERANK.valid_order({ 0 }, 2), '长度不符')
    assert_true(not RERANK.valid_order({ 0, 2 }, 2), '越界')
    assert_true(not RERANK.valid_order({ 0, '1' }, 2), '类型不符')
    assert_true(not RERANK.valid_order(nil, 2), 'nil')
    assert_true(not RERANK.valid_order({}, 0), '空排列')
end)

test('apply_order 回填到原位置且不动的候选保持不动', function()
    local candidates = make_candidates({ '你好', '，', '尼豪', '拟好' }, { 'sentence', 'punct', 'sentence', 'sentence' })
    local picked, positions = RERANK.collect(candidates, 8)
    assert_eq(3, #picked)
    RERANK.apply_order(candidates, positions, { 2, 0, 1 })
    assert_eq('拟好', candidates[1].text)
    assert_eq('，', candidates[2].text)
    assert_eq('你好', candidates[3].text)
    assert_eq('尼豪', candidates[4].text)
end)

test('annotate_scores 标注概率并给模型首选打星', function()
    local candidates = make_candidates({ '你好', '尼豪', '拟好' })
    local positions = { 1, 2, 3 }
    local scores = { ['0'] = 0.8712, ['1'] = 0.0834, ['2'] = 0.0454 }
    assert_eq(3, RERANK.annotate_scores(candidates, positions, scores, 0))
    assert_eq('★87%', candidates[1].comment)
    assert_eq('8%', candidates[2].comment)
    assert_eq('5%', candidates[3].comment)
    assert_eq('你好', candidates[1].text, '对比模式不该改顺序')

    -- 模型首选在第三位时星星落在第三位: 一眼看出词库把它排后面了
    local other = make_candidates({ '你好', '尼豪', '拟好' })
    RERANK.annotate_scores(other, positions, { ['0'] = 0.1, ['1'] = 0.2, ['2'] = 0.7 }, 2)
    assert_eq('10%', other[1].comment)
    assert_eq('★70%', other[3].comment)
end)

test('annotate_scores 保留原注释且不重复追加', function()
    local candidates = make_candidates({ '你好', '尼豪' })
    candidates[1].comment = '〔user〕'
    RERANK.annotate_scores(candidates, { 1, 2 }, { ['0'] = 0.9, ['1'] = 0.1 }, 0)
    assert_eq('〔user〕 ★90%', candidates[1].comment)
    RERANK.annotate_scores(candidates, { 1, 2 }, { ['0'] = 0.9, ['1'] = 0.1 }, 0)
    assert_eq('〔user〕 ★90%', candidates[1].comment)

    -- 缺概率的槽位跳过, 不写空注释
    local partial = make_candidates({ '你好', '尼豪' })
    assert_eq(1, RERANK.annotate_scores(partial, { 1, 2 }, { ['0'] = 0.9 }, 0))
    assert_eq('', partial[2].comment)
    assert_eq(0, RERANK.annotate_scores(partial, { 1, 2 }, nil, 0))
end)

test('apply_badge 追加一次且不覆盖已有注释', function()
    local candidates = make_candidates({ '你好', '尼豪' })
    candidates[1].comment = '〔user〕'
    assert_true(RERANK.apply_badge(candidates, 'AI'))
    assert_eq('〔user〕 AI', candidates[1].comment)
    assert_true(not RERANK.apply_badge(candidates, 'AI'), '不应重复追加')
    assert_eq('〔user〕 AI', candidates[1].comment)

    local plain = make_candidates({ '你好' })
    RERANK.apply_badge(plain, 'AI')
    assert_eq('AI', plain[1].comment)

    local empty = make_candidates({ '你好' })
    assert_true(not RERANK.apply_badge(empty, ''))
    assert_eq('', empty[1].comment)
end)
