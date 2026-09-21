-- jev/jev_json.lua
-- 极简 JSON 编解码, 只覆盖 jev-bridge 的请求与响应结构.
-- 不引第三方库: librime-lua 只内建了 utf8, 而 LuaSocket/cjson 都不在包里.

local M = {}

M.array_mt = { __json_array = true }

--- 标记为数组 (元素个数可以少于最大下标时必须显式标记)
function M.array(tbl)
    return setmetatable(tbl or {}, M.array_mt)
end

local ESCAPES = {
    ['"'] = '\\"',
    ['\\'] = '\\\\',
    ['\b'] = '\\b',
    ['\f'] = '\\f',
    ['\n'] = '\\n',
    ['\r'] = '\\r',
    ['\t'] = '\\t',
}

local function encode_string(text)
    return '"' .. text:gsub('[%c\\"]', function(char)
        return ESCAPES[char] or string.format('\\u%04x', char:byte())
    end) .. '"'
end

local function encode_number(value)
    if math.type(value) == 'integer' then
        return string.format('%d', value)
    end
    if value ~= value or value == math.huge or value == -math.huge then
        return 'null'
    end
    return string.format('%.6f', value)
end

local function is_array(tbl)
    if getmetatable(tbl) == M.array_mt then
        return true
    end
    local count = 0
    for key in pairs(tbl) do
        if type(key) ~= 'number' or key ~= math.floor(key) or key < 1 then
            return false
        end
        count = count + 1
    end
    return count == #tbl
end

local encode_value

local function encode_table(tbl)
    if is_array(tbl) then
        local parts = {}
        for index = 1, #tbl do
            parts[index] = encode_value(tbl[index])
        end
        return '[' .. table.concat(parts, ',') .. ']'
    end
    local keys = {}
    for key in pairs(tbl) do
        keys[#keys + 1] = tostring(key)
    end
    table.sort(keys)
    local parts = {}
    for _, key in ipairs(keys) do
        parts[#parts + 1] = encode_string(key) .. ':' .. encode_value(tbl[key])
    end
    return '{' .. table.concat(parts, ',') .. '}'
end

encode_value = function(value)
    local kind = type(value)
    if value == nil then
        return 'null'
    elseif kind == 'boolean' then
        return value and 'true' or 'false'
    elseif kind == 'number' then
        return encode_number(value)
    elseif kind == 'string' then
        return encode_string(value)
    elseif kind == 'table' then
        return encode_table(value)
    end
    return 'null'
end

function M.encode(value)
    local ok, text = pcall(encode_value, value)
    if not ok then
        return nil, tostring(text)
    end
    return text
end

-- ---------------------------------------------------------------- 解码

local function utf8_char(codepoint)
    if codepoint < 0x80 then
        return string.char(codepoint)
    elseif codepoint < 0x800 then
        return string.char(
            0xC0 + math.floor(codepoint / 0x40),
            0x80 + codepoint % 0x40
        )
    elseif codepoint < 0x10000 then
        return string.char(
            0xE0 + math.floor(codepoint / 0x1000),
            0x80 + math.floor(codepoint / 0x40) % 0x40,
            0x80 + codepoint % 0x40
        )
    end
    return string.char(
        0xF0 + math.floor(codepoint / 0x40000),
        0x80 + math.floor(codepoint / 0x1000) % 0x40,
        0x80 + math.floor(codepoint / 0x40) % 0x40,
        0x80 + codepoint % 0x40
    )
end

local function decode(text)
    local position = 1
    local length = #text

    local function fail(message)
        error(string.format('JSON 解析失败 (第 %d 字节): %s', position, message), 0)
    end

    local function skip_space()
        while position <= length do
            local char = text:sub(position, position)
            if char == ' ' or char == '\n' or char == '\r' or char == '\t' then
                position = position + 1
            else
                break
            end
        end
    end

    local function parse_string()
        position = position + 1 -- 跳过开引号
        local buffer = {}
        while true do
            if position > length then
                fail('字符串没有结束')
            end
            local char = text:sub(position, position)
            if char == '"' then
                position = position + 1
                return table.concat(buffer)
            elseif char == '\\' then
                local escape = text:sub(position + 1, position + 1)
                if escape == 'u' then
                    local hex = text:sub(position + 2, position + 5)
                    if not hex:match('^%x%x%x%x$') then
                        fail('\\u 转义不合法')
                    end
                    local codepoint = tonumber(hex, 16)
                    position = position + 6
                    if codepoint >= 0xD800 and codepoint <= 0xDBFF then
                        local low_hex = text:sub(position, position + 5)
                        local low = low_hex:match('^\\u(%x%x%x%x)$')
                        if low then
                            low = tonumber(low, 16)
                            if low >= 0xDC00 and low <= 0xDFFF then
                                codepoint = 0x10000 + (codepoint - 0xD800) * 0x400 + (low - 0xDC00)
                                position = position + 6
                            end
                        end
                    end
                    buffer[#buffer + 1] = utf8_char(codepoint)
                else
                    local mapped = ({
                        ['"'] = '"', ['\\'] = '\\', ['/'] = '/', b = '\b',
                        f = '\f', n = '\n', r = '\r', t = '\t',
                    })[escape]
                    if not mapped then
                        fail('未知转义 \\' .. tostring(escape))
                    end
                    buffer[#buffer + 1] = mapped
                    position = position + 2
                end
            else
                buffer[#buffer + 1] = char
                position = position + 1
            end
        end
    end

    local function parse_number()
        local number = text:match('^-?%d+%.?%d*[eE]?[+-]?%d*', position)
        if not number or number == '' then
            fail('数字不合法')
        end
        position = position + #number
        return tonumber(number)
    end

    local parse_value

    local function parse_array()
        position = position + 1
        local result = {}
        skip_space()
        if text:sub(position, position) == ']' then
            position = position + 1
            return result
        end
        while true do
            result[#result + 1] = parse_value()
            skip_space()
            local char = text:sub(position, position)
            if char == ',' then
                position = position + 1
                skip_space()
            elseif char == ']' then
                position = position + 1
                return result
            else
                fail('数组里期望 , 或 ]')
            end
        end
    end

    local function parse_object()
        position = position + 1
        local result = {}
        skip_space()
        if text:sub(position, position) == '}' then
            position = position + 1
            return result
        end
        while true do
            skip_space()
            if text:sub(position, position) ~= '"' then
                fail('对象的键必须是字符串')
            end
            local key = parse_string()
            skip_space()
            if text:sub(position, position) ~= ':' then
                fail('对象缺少冒号')
            end
            position = position + 1
            skip_space()
            result[key] = parse_value()
            skip_space()
            local char = text:sub(position, position)
            if char == ',' then
                position = position + 1
            elseif char == '}' then
                position = position + 1
                return result
            else
                fail('对象里期望 , 或 }')
            end
        end
    end

    parse_value = function()
        skip_space()
        local char = text:sub(position, position)
        if char == '{' then
            return parse_object()
        elseif char == '[' then
            return parse_array()
        elseif char == '"' then
            return parse_string()
        elseif char == 't' and text:sub(position, position + 3) == 'true' then
            position = position + 4
            return true
        elseif char == 'f' and text:sub(position, position + 4) == 'false' then
            position = position + 5
            return false
        elseif char == 'n' and text:sub(position, position + 3) == 'null' then
            position = position + 4
            return nil
        end
        return parse_number()
    end

    local value = parse_value()
    skip_space()
    if position <= length then
        fail('结尾有多余内容')
    end
    return value
end

function M.decode(text)
    if type(text) ~= 'string' or text == '' then
        return nil, '空输入'
    end
    local ok, value = pcall(decode, text)
    if not ok then
        return nil, tostring(value)
    end
    return value
end

return M
