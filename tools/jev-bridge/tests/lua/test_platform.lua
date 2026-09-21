-- 平台路径推导的测试 (规则必须与 src/jev_bridge/paths.py 一致, 由 Python 侧的
-- test_paths.py::test_lua_and_python_agree_on_runtime_dir 做跨语言核对)

local PLATFORM = require('jev/jev_platform')
local DEFAULTS = require('jev/defaults')

test('默认运行时目录符合当前平台规则', function()
    local home = PLATFORM.home_dir()
    local runtime = PLATFORM.runtime_dir()
    local platform = PLATFORM.platform_name()

    if platform == 'windows' then
        assert_true(runtime:find('rime%-jev$') ~= nil, 'runtime=' .. runtime)
    elseif platform == 'macos' then
        assert_eq(home .. '/Library/Caches/rime-jev', runtime)
    else
        local xdg = os.getenv('XDG_CACHE_HOME')
        local expected
        if xdg and xdg ~= '' then
            expected = xdg:gsub('\\', '/') .. '/rime-jev'
        else
            expected = home .. '/.cache/rime-jev'
        end
        assert_eq(expected, runtime)
    end

    -- defaults 与 client 都必须用同一份推导结果
    assert_eq(runtime, DEFAULTS.runtime_dir)
    assert_eq(runtime .. '/queue', DEFAULTS.queue_dir)
end)

test('dir_exists 能区分存在与不存在的目录', function()
    assert_true(PLATFORM.dir_exists(RIME_DIR), '仓库目录应当存在')
    assert_true(not PLATFORM.dir_exists('/tmp/definitely-not-here-' .. tostring(os.time())))
end)
