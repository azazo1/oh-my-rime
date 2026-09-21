-- tests/lua/run.lua
-- 极简测试运行器 (不引 busted):
--   lua tests/lua/run.lua <rime_dir> [tmp_dir]
-- rime_dir 用于定位 lua/jev/*.lua; tmp_dir 用作队列与缓存的沙箱目录.

local rime_dir = arg[1]
local tmp_dir = arg[2] or '/tmp/jev-lua-suite'

if not rime_dir then
    io.stderr:write('用法: lua run.lua <rime_dir> [tmp_dir]\n')
    os.exit(2)
end

package.path = table.concat({
    rime_dir .. '/lua/?.lua',
    rime_dir .. '/lua/?/init.lua',
    package.path,
}, ';')

local JSON = require('jev/jev_json')
local CLIENT = require('jev/jev_client')

-- 所有测试都在这个沙箱根目录下跑: 过滤器的 init 会按配置调用 set_runtime_dir,
-- 所以测试里的 runtime_dir 必须指向同一个根, 否则请求会被写到真实用户目录去.
local runtime_root = tmp_dir .. '/runtime'
os.execute('rm -rf "' .. tmp_dir .. '" && mkdir -p "' .. tmp_dir .. '"')
CLIENT.set_runtime_dir(runtime_root)

_G.RIME_DIR = rime_dir
_G.TMP_DIR = tmp_dir
_G.RUNTIME_DIR = runtime_root
_G.FIXTURE_PATH = rime_dir .. '/tools/jev-bridge/tests/fixtures/key_vectors.json'
_G.JSON = JSON
_G.CLIENT = CLIENT

local tests = {}
local failures = 0
local passed = 0

function _G.test(name, fn)
    tests[#tests + 1] = { name = name, fn = fn }
end

function _G.assert_eq(expected, actual, label)
    if expected ~= actual then
        error(string.format(
            '%s: 期望 %s, 实际 %s',
            label or 'assert_eq',
            tostring(expected),
            tostring(actual)
        ), 2)
    end
end

function _G.assert_true(value, label)
    if not value then
        error((label or 'assert_true') .. ': 期望为真', 2)
    end
end

function _G.assert_nil(value, label)
    if value ~= nil then
        error(string.format('%s: 期望 nil, 实际 %s', label or 'assert_nil', tostring(value)), 2)
    end
end

local script_dir = arg[0]:match('^(.*)/[^/]*$') or '.'
for _, file in ipairs({
    'test_json.lua',
    'test_client.lua',
    'test_rerank.lua',
    'test_platform.lua',
    'test_filter.lua',
}) do
    dofile(script_dir .. '/' .. file)
end

for _, case in ipairs(tests) do
    local ok, err = pcall(case.fn)
    if ok then
        passed = passed + 1
        print('PASS ' .. case.name)
    else
        failures = failures + 1
        print('FAIL ' .. case.name .. ': ' .. tostring(err))
    end
end

print(string.format('Lua 测试: %d 通过, %d 失败', passed, failures))
os.exit(failures == 0 and 0 or 1)
