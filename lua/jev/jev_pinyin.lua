-- jev/jev_pinyin.lua
-- 把双拼按键串展开成全拼, 让模型看得懂用户到底在打什么.
--
-- 为什么要自己做: schema 里的双拼规则是一串正向改写 (xform/xlit, 全拼 -> 按键), 反推不可靠;
-- 而"按键 -> 韵母"是固定键位表, 直接实现更稳. 目前支持小鹤双拼 (flypy).
-- 规则依据 wanxiang_algebra.yaml 的 base/小鹤双拼 段 (例如零声母写作 aj/ac/ad/ew/ef/oz,
-- a/o/e 单韵母双写, zh/ch/sh 落在 v/i/u 三个键上).
--
-- 展开结果只作为提示发给模型 (state.pinyin); 解析失败返回 nil, 不影响原有链路.

local M = {}

-- 声母: 小鹤里 zh/ch/sh 各占一个键
local INITIAL_KEYS = { v = 'zh', i = 'ch', u = 'sh' }
local CONSONANTS = 'bpmfdtnlgkhjqxrzcsyw'
local PLACEHOLDERS = 'aoe'

-- 韵母键位表; 少数键在不同声母下读音不同, 交给 resolve_final 判定
local SIMPLE_FINALS = {
    a = 'a', b = 'in', c = 'ao', d = 'ai', e = 'e', f = 'en', g = 'eng', h = 'ang',
    i = 'i', j = 'an', k = 'ing', l = 'uang', m = 'ian', n = 'iao', o = 'o', p = 'ie',
    q = 'iu', r = 'uan', s = 'ong', t = 'ue', u = 'u', w = 'ei', x = 'ua', y = 'un',
    z = 'ou',
}

-- y/w 开头的音节写法会吃掉韵母里的 i/u, 逐个列出避免出错
local SPELLING_FIXUPS = {
    ['y|ia'] = 'ya', ['y|ie'] = 'ye', ['y|iao'] = 'yao', ['y|iu'] = 'you',
    ['y|iong'] = 'yong',
    ['w|ua'] = 'wa', ['w|uo'] = 'wo', ['w|uai'] = 'wai', ['w|uan'] = 'wan',
    ['w|uang'] = 'wang', ['w|un'] = 'wen', ['w|u'] = 'wu', ['w|ei'] = 'wei',
}

local function in_list(list, value)
    for _, item in ipairs(list) do
        if item == value then return true end
    end
    return false
end

--- 某个韵母键在给定声母下读什么
local function resolve_final(key, initial)
    if key == 'l' then                        -- iang / uang
        return in_list({ 'j', 'q', 'x', 'n', 'l', 'y' }, initial) and 'iang' or 'uang'
    elseif key == 'r' then                    -- uan / er (零声母)
        return initial == '' and 'er' or 'uan'
    elseif key == 'x' then                    -- ia / ua
        return in_list({ 'j', 'q', 'x', 'y' }, initial) and 'ia' or 'ua'
    elseif key == 's' then                    -- iong / ong
        return in_list({ 'j', 'q', 'x', 'y' }, initial) and 'iong' or 'ong'
    elseif key == 'o' then                    -- o / uo
        return in_list({ '', 'b', 'p', 'm', 'f' }, initial) and 'o' or 'uo'
    elseif key == 'v' then                    -- ui / ü (写成 v, jqx 后写成 u)
        if in_list({ 'n', 'l' }, initial) then return 'v' end
        if in_list({ 'j', 'q', 'x', 'y' }, initial) then return 'u' end
        return 'ui'
    elseif key == 't' then                    -- üe (写成 ue, nl 后写成 ve)
        return in_list({ 'n', 'l' }, initial) and 've' or 'ue'
    elseif key == 'k' then                    -- ing / uai
        return in_list({ 'b', 'p', 'm', 'd', 't', 'n', 'l', 'j', 'q', 'x', 'y' }, initial)
            and 'ing' or 'uai'
    end
    return SIMPLE_FINALS[key]
end

local function spell(initial, final)
    local fixed = SPELLING_FIXUPS[initial .. '|' .. final]
    return fixed or (initial .. final)
end

--- 展开一个双拼按键串; 无法解析时返回 nil.
-- 返回 (完整字符串, 音节数组), 例如 nihc -> ("ni hao", {"ni", "hao"}).
function M.expand(code)
    if type(code) ~= 'string' or code == '' then return nil end
    if code:find('[^a-z]') then return nil end

    local syllables = {}
    local index = 1
    while index <= #code do
        local first = code:sub(index, index)
        local second = code:sub(index + 1, index + 1)
        local initial, final

        if PLACEHOLDERS:find(first, 1, true) and index + 1 <= #code then
            -- 零声母: "aj"=an, "ac"=ao, "ad"=ai, "ew"=ei, "ef"=en, "oz"=ou, "aa"=a
            initial = ''
            if second == first then
                final = first
            else
                final = resolve_final(second, '')
                if not final then return nil end
            end
            index = index + 2
        elseif INITIAL_KEYS[first] then
            initial = INITIAL_KEYS[first]
            final = resolve_final(second, initial)
            index = index + 2
        elseif first:find('[' .. CONSONANTS .. ']') then
            initial = first
            final = resolve_final(second, initial)
            index = index + 2
        else
            return nil
        end

        if not final or second == '' then
            return nil
        end
        syllables[#syllables + 1] = spell(initial, final)
    end

    if #syllables == 0 then return nil end
    return table.concat(syllables, ' '), syllables
end

return M
