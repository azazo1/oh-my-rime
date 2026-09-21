-- jev/jev_platform.lua
-- 按平台推导默认路径, 规则必须与 tools/jev-bridge/src/jev_bridge/paths.py 完全一致
-- (不一致时 sidecar 的 `jev-bridge paths` 会报两侧 runtime_dir 不匹配).
--
--   macOS   ~/Library/Caches/rime-jev
--   Linux   $XDG_CACHE_HOME/rime-jev 或 ~/.cache/rime-jev
--   Windows %LOCALAPPDATA%\rime-jev

local M = {}

-- Windows 下 os.execute 走 cmd.exe, 建目录与串联命令的写法都不同
M.is_windows = package.config:sub(1, 1) == '\\'

function M.home_dir()
    return os.getenv('HOME') or os.getenv('USERPROFILE') or '/tmp'
end

--- 路径是否存在.
-- 用 io.open 而不是 os.rename(p, p): 后者在本机 (macOS + Lua 5.4.8) 对同路径返回 nil,
-- 而 io.open 对目录会成功返回句柄 (读不出来但足以判断存在), Linux 行为一致.
function M.dir_exists(path)
    local handle = io.open(path, 'r')
    if not handle then
        return false
    end
    handle:close()
    return true
end

function M.platform_name()
    if M.is_windows then
        return 'windows'
    end
    local code_name = ''
    if rime_api and rime_api.get_distribution_code_name then
        local ok, value = pcall(rime_api.get_distribution_code_name)
        if ok and type(value) == 'string' then
            code_name = value:lower()
        end
    end
    -- Squirrel 是 macOS 前端; 取不到前端名时退化成探测 macOS 特有的缓存目录
    if code_name:find('squirrel', 1, true) or M.dir_exists(M.home_dir() .. '/Library/Caches') then
        return 'macos'
    end
    return 'linux'
end

--- 队列/缓存/日志共同的父目录 (仓库之外, 不进版本控制).
function M.runtime_dir()
    local override = os.getenv('JEV_RUNTIME_DIR')
    if override and override ~= '' then
        return (override:gsub('\\', '/'))
    end
    if M.is_windows then
        local base = os.getenv('LOCALAPPDATA')
        if base and base ~= '' then
            return (base:gsub('\\', '/')) .. '/rime-jev'
        end
        return M.home_dir() .. '/AppData/Local/rime-jev'
    end
    if M.platform_name() == 'macos' then
        return M.home_dir() .. '/Library/Caches/rime-jev'
    end
    local xdg = os.getenv('XDG_CACHE_HOME')
    if xdg and xdg ~= '' then
        return (xdg:gsub('\\', '/')) .. '/rime-jev'
    end
    return M.home_dir() .. '/.cache/rime-jev'
end

return M
