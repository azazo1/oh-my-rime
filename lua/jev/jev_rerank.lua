-- jev/jev_rerank.lua
-- 纯逻辑: 组装请求, 校验 sidecar 返回的顺序, 回填候选并追加标记.
-- 这里不碰 librime 的 API, 便于用普通 lua 解释器直接跑单元测试.

local JSON = require('jev/jev_json')
local CLIENT = require('jev/jev_client')
local DEFAULTS = require('jev/defaults')

local M = {}

--- 取出参与重排的候选.
-- 返回 (picked, positions): picked 是候选对象, positions 是它们在原列表里的下标.
function M.collect(candidates, max_count)
    local picked, positions = {}, {}
    for index = 1, #candidates do
        if #picked >= max_count then break end
        local candidate = candidates[index]
        if candidate and candidate.type ~= 'punct' then
            picked[#picked + 1] = candidate
            positions[#positions + 1] = index
        end
    end
    return picked, positions
end

--- 组装请求, 返回 (request, cache_key).
function M.build_request(opts)
    local texts = {}
    local candidates = JSON.array({})
    local criteria = {}
    for slot = 1, #opts.picked do
        local candidate = opts.picked[slot]
        local text = CLIENT.normalize_text(candidate.text or '')
        texts[slot] = text
        candidates[slot] = {
            index = slot - 1,
            text = text,
            type = candidate.type or '',
        }
        criteria[tostring(slot - 1)] = text
    end
    local context = CLIENT.normalize_context(opts.context or '', opts.context_chars)
    local key = CLIENT.cache_key(opts.schema_id, opts.code, context, texts, opts.prompt_version)
    local request = {
        v = DEFAULTS.protocol_version,
        id = CLIENT.new_id(),
        ts = os.time(),
        mode = opts.mode,
        schema_id = opts.schema_id,
        code = opts.code,
        context = context,
        candidates = candidates,
        question = {
            name = 'best_continuation',
            type = 'choice',
            instructions = opts.instructions,
            criteria = criteria,
        },
        prompt_version = opts.prompt_version,
        cache_key = key,
        badge = opts.badge,
        show_confidence = opts.show_confidence and true or false,
    }
    return request, key
end

--- order 必须是 0..count-1 的一个排列, 否则一律当作无效结果忽略.
function M.valid_order(order, count)
    if type(order) ~= 'table' or #order ~= count or count == 0 then
        return false
    end
    local seen = {}
    for _, value in ipairs(order) do
        if type(value) ~= 'number' or value < 0 or value >= count then
            return false
        end
        if value ~= math.floor(value) or seen[value] then
            return false
        end
        seen[value] = true
    end
    return true
end

--- 按 order 把 picked 的新顺序回填到原列表的对应位置上.
function M.apply_order(candidates, positions, order)
    local picked = {}
    for slot = 1, #positions do
        picked[slot] = candidates[positions[slot]]
    end
    for slot = 1, #order do
        candidates[positions[slot]] = picked[order[slot] + 1]
    end
end

--- 把标记追加到首位候选的注释尾部; 已有同样标记时不重复追加.
function M.apply_badge(candidates, badge)
    if not badge or badge == '' then return false end
    local candidate = candidates[1]
    if not candidate then return false end
    local comment = candidate.comment or ''
    if comment:find(badge, 1, true) then return false end
    candidate.comment = (comment == '') and badge or (comment .. ' ' .. badge)
    return true
end

--- 对比模式: 不动顺序, 只在每个候选的注释里写上模型给它的概率, 并在模型的首选前加星.
-- 这样一屏就能看出 "词库按词频排的顺序" 与 "模型想排的顺序" 差在哪.
function M.annotate_scores(candidates, positions, scores, top_index)
    if not scores then return 0 end
    local annotated = 0
    for slot = 1, #positions do
        local candidate = candidates[positions[slot]]
        local score = scores[tostring(slot - 1)]
        if candidate and type(score) == 'number' then
            local text = string.format('%d%%', math.floor(score * 100 + 0.5))
            if top_index ~= nil and (slot - 1) == top_index then
                text = '★' .. text
            end
            local comment = candidate.comment or ''
            if not comment:find(text, 1, true) then
                candidate.comment = (comment == '') and text or (comment .. ' ' .. text)
                annotated = annotated + 1
            end
        end
    end
    return annotated
end

return M
