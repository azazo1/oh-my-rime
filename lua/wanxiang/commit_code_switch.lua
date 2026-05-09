local wanxiang = require("wanxiang/wanxiang")

local M = {}

local function get_process_result(name)
    return wanxiang.RIME_PROCESS_RESULTS[name]
end

local function get_string(map, key)
    local value = map:get_value(key)
    if not value then
        return nil
    end
    return value:get_string()
end

local function parse_binding(map)
    local accept = get_string(map, "accept")
    if not accept or accept == "" then
        return nil
    end

    local action = nil
    local toggle = get_string(map, "toggle")
    local set_option = get_string(map, "set_option")
    local unset_option = get_string(map, "unset_option")

    if toggle == "ascii_mode" then
        action = "toggle"
    elseif set_option == "ascii_mode" then
        action = "set"
    elseif unset_option == "ascii_mode" then
        action = "unset"
    end

    if not action then
        return nil
    end

    return {
        accept = KeyEvent(accept),
        action = action,
        when = get_string(map, "when") or "always",
    }
end

local function should_run(binding, ctx)
    local when = binding.when
    if when == "always" then
        return true
    end
    if when == "composing" then
        return ctx:is_composing()
    end
    if when == "has_menu" or when == "paging" then
        return ctx:has_menu()
    end
    return false
end

local function get_target_ascii_mode(binding, current)
    if binding.action == "toggle" then
        return not current
    end
    if binding.action == "set" then
        return true
    end
    return false
end

function M.init(env)
    env.bindings = {}
    env.enabled = false

    local config = env.engine.schema.config
    if not config then
        return
    end

    local enabled = config:get_bool("commit_code_switch/enabled")
    if not enabled then
        return
    end
    env.enabled = true

    local bindings = config:get_list("key_binder/bindings")
    if not bindings then
        return
    end

    for i = 0, bindings.size - 1 do
        local item = bindings:get_at(i)
        local map = item and item:get_map() or nil
        if map then
            local binding = parse_binding(map)
            if binding then
                table.insert(env.bindings, binding)
            end
        end
    end
end

function M.func(key_event, env)
    if not env.enabled then
        return get_process_result("kNoop")
    end

    local ctx = env.engine.context
    if not ctx then
        return get_process_result("kNoop")
    end

    for _, binding in ipairs(env.bindings) do
        if key_event:eq(binding.accept) and should_run(binding, ctx) then
            local current = ctx:get_option("ascii_mode")
            local target = get_target_ascii_mode(binding, current)
            if target and not current and ctx:is_composing() then
                local input = ctx.input or ""
                if input ~= "" then
                    env.engine:commit_text(input)
                    ctx:clear()
                end
            end
            ctx:set_option("ascii_mode", target)
            return get_process_result("kAccepted")
        end
    end

    return get_process_result("kNoop")
end

return M
