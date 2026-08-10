-- Chat shim: wraps ParseInputChatString in the ChatPanel state so the human's
-- typed messages are routed to the Python message mailbox.  In single-player
-- Network.SendChat has no network to send to, but it DOES echo the message
-- locally (so the human sees their own line in the chat log); this wrapper
-- adds the mailbox routing on top without duplicating that echo.
--
-- This file is a TEMPLATE loaded by build_chat_shim_install_lua() in
-- handoff.py.  One tag is substituted before the Lua is sent to the game:
--   __MCP_SENTINEL_TAG__  -> the response sentinel (see _helpers.SENTINEL)
--
-- Why ParseInputChatString and not Network.SendChat?
--   Network is a READ-ONLY table in the ChatPanel state (assigning to
--   Network.SendChat raises "Attempt to modify read-only table").
--   ParseInputChatString, by contrast, is a plain global function (declared
--   in ChatLogic.lua, which is include()'d into the ChatPanel state), so it
--   is writable — the same kind of hook the deal shim uses for IsAutoPropose
--   and UpdateDealStatus.  It is called exactly once per message, inside
--   SendChat (ChatPanel.lua:301), on Enter-key commit.
--
-- Idempotent re-arm: the original is captured once (when
-- __MCP_orig_ParseInputChatString is nil) and called through a LOCAL upvalue,
-- so repeated installs never stack wrappers and can never self-recurse.  Same
-- pattern as deal_shim.lua.

__MCP_chat_install_count = (__MCP_chat_install_count or 0) + 1

if __MCP_orig_ParseInputChatString == nil then
    __MCP_orig_ParseInputChatString = ParseInputChatString
end
local origPICT = __MCP_orig_ParseInputChatString

ParseInputChatString = function(chatText, playerTargetData)
    -- Delegate first so slash-command parsing still runs and SendChat
    -- continues normally (ClearString, sent sound, Network.SendChat).
    local parsedText, chatTargetChanged, printHelp =
        origPICT(chatText, playerTargetData)

    -- Route the parsed text to the Python mailbox.  parsedText is empty for
    -- pure slash-commands (e.g. "/t" mode switches) — skip those.
    -- We do NOT echo the human's own message here: in single-player
    -- Network.SendChat (called by SendChat after we return) echoes the
    -- message locally itself, so a second OnChat would duplicate it.
    if parsedText and parsedText ~= "" then
        local me = Game.GetLocalPlayer()
        local hex = ""
        for i = 1, #parsedText do
            hex = hex .. string.format("%02x", string.byte(parsedText, i))
        end
        print("MCPCHAT|SEND"
            .. "|from=" .. tostring(me)
            .. "|hex=" .. hex)
    end

    return parsedText, chatTargetChanged, printHelp
end

print("CHATSHIM|installed|install_count=" .. tostring(__MCP_chat_install_count))
print("__MCP_SENTINEL_TAG__")
