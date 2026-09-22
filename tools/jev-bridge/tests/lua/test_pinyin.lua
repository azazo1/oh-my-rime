-- 双拼展开的测试 (小鹤双拼键位表)
local PINYIN = require('jev/jev_pinyin')

local function expand(code)
    local full = PINYIN.expand(code)
    return full
end

test('小鹤双拼: 普通音节', function()
    assert_eq('ni hao', expand('nihc'))          -- ni + h(c=ao)
    assert_eq('zhong guo', expand('vsgo'))       -- v=zh, s=ong
    assert_eq('shi jian', expand('uijm'))        -- u=sh, m=ian
    assert_eq('chi fan', expand('iifj'))         -- i=ch, j=an
    assert_eq('bei jing', expand('bwjk'))
end)

test('小鹤双拼: 有歧义的韵母键靠声母判定', function()
    assert_eq('liang', expand('ll'))             -- l 在 l 声母下是 iang
    assert_eq('guang', expand('gl'))             -- 在 g 声母下是 uang
    assert_eq('juan', expand('jr'))              -- r 在 j 后是 uan
    assert_eq('xue', expand('xt'))               -- t 是 ue
    assert_eq('nv', expand('nv'))                -- v 在 n 后是 ü
    assert_eq('ju', expand('jv'))                -- j 后的 ü 写成 u
    assert_eq('dui', expand('dv'))               -- 其它声母后 v 是 ui
    assert_eq('xia', expand('xx'))               -- x 在 x 声母下是 ia
    assert_eq('hua', expand('hx'))               -- 在 h 声母下是 ua
    assert_eq('yong', expand('ys'))              -- y 后的 iong 去掉 i
    assert_eq('wai', expand('wk'))               -- k 在 w 后是 uai
    assert_eq('ying', expand('yk'))
end)

test('小鹤双拼: 零声母与单韵母', function()
    assert_eq('an', expand('aj'))
    assert_eq('ao', expand('ac'))
    assert_eq('ai', expand('ad'))
    assert_eq('ei', expand('ew'))
    assert_eq('en', expand('ef'))
    assert_eq('ou', expand('oz'))
    assert_eq('a', expand('aa'))                 -- a/o/e 双写
    assert_eq('e', expand('ee'))
    assert_eq('er', expand('er'))
    assert_eq('yi', expand('yi'))
    assert_eq('wu', expand('wu'))
    assert_eq('ya', expand('yx'))
    assert_eq('wen', expand('wy'))
end)

test('展开失败时返回 nil 而不是瞎猜', function()
    assert_nil(expand(''))
    assert_nil(expand('nih1'))
    assert_nil(expand('a'))       -- 单个占位字母不成音节
    assert_nil(expand(nil))
end)
