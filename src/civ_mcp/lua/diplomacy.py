"""Diplomacy domain — Lua builders and parsers."""

from __future__ import annotations

from civ_mcp.lua._helpers import (
    SENTINEL,
    _bail,
    _bail_lua,
    _lua_close_diplo_session,
    load_lua_template,
)
from civ_mcp.lua.models import (
    AgendaInfo,
    CivInfo,
    DealItem,
    DealOptions,
    DiplomacyModifier,
    DiplomacySession,
    OwnAbilities,
    PendingDeal,
    TestTradeItem,
    TestTradeResult,
    TradeableCity,
    TraitInfo,
    UniqueInfo,
    VisibleCity,
)

# ---------------------------------------------------------------------------
# Diplomatic-action constants (shared by the engine builder and the mailbox)
# ---------------------------------------------------------------------------

# Actions whose target gets to accept or reject. Only these are mailbox-routed
# to managed civs; one-way actions (denounce, war declarations) always go
# straight to the engine.
RESPONSEABLE_DIPLO_ACTIONS: frozenset[str] = frozenset(
    {"DECLARE_FRIENDSHIP", "DIPLOMATIC_DELEGATION", "RESIDENT_EMBASSY"}
)

# action_name -> DiplomacyManager.RequestSession string. Single source of truth
# for build_send_diplo_action and the diplo mailbox (which must open the same
# session type when the proposer later executes an accepted proposal).
DIPLO_SESSION_STRING_MAP: dict[str, str] = {
    "DECLARE_FRIENDSHIP": "DECLARE_FRIEND",
    "DIPLOMATIC_DELEGATION": "DIPLOMATIC_DELEGATION",
    "RESIDENT_EMBASSY": "RESIDENT_EMBASSY",
    "DENOUNCE": "DENOUNCE",
    # War declarations — session strings match action names.
    "DECLARE_SURPRISE_WAR": "DECLARE_SURPRISE_WAR",
    "DECLARE_FORMAL_WAR": "DECLARE_FORMAL_WAR",
    "DECLARE_HOLY_WAR": "DECLARE_HOLY_WAR",
    "DECLARE_LIBERATION_WAR": "DECLARE_LIBERATION_WAR",
    "DECLARE_RECONQUEST_WAR": "DECLARE_RECONQUEST_WAR",
    "DECLARE_PROTECTORATE_WAR": "DECLARE_PROTECTORATE_WAR",
    "DECLARE_COLONIAL_WAR": "DECLARE_COLONIAL_WAR",
    "DECLARE_TERRITORIAL_WAR": "DECLARE_TERRITORIAL_WAR",
}

# Inverse map: session string -> action_name. The diplo shim reports the
# session string the UI passed to RequestSession (e.g. "DECLARE_FRIEND"),
# which maps back to the mailbox's action_name (e.g. DECLARE_FRIENDSHIP).
SESSION_STRING_TO_ACTION: dict[str, str] = {
    v: k for k, v in DIPLO_SESSION_STRING_MAP.items()
}

# action_name -> DiplomacyActionTypes enum key (the InGame context's enum
# table) used by DiplomacyManager.SendAction to prime an orphan session so
# the DiplomacyActionView adopts it. Live-verified enum keys
# (DIPLO_EXECUTION_PLAN.md §2): DECLARE_FRIEND / SET_DELEGATION / SET_EMBASSY.
DIPLO_ACTION_TO_ENUM: dict[str, str] = {
    "DECLARE_FRIENDSHIP": "DECLARE_FRIEND",
    "DIPLOMATIC_DELEGATION": "SET_DELEGATION",
    "RESIDENT_EMBASSY": "SET_EMBASSY",
}


# Lua helper injected into queries that emit trait/unique text. Strips the raw
# ``[ICON_*]``, ``[NEWLINE]`` and ``[COLOR]`` markup tokens that Locale.Lookup
# leaves in ability/unique descriptions, and collapses whitespace, so the
# rendered text is readable for the LLM. Each section runs inside its own
# pcall(function() ... end) scope in the unified query, so the ``local``
# declaration does not collide across sections.
_LUA_CLEAN_TEXT = """
local function _cleanText(s)
    if s == nil then return "" end
    s = tostring(s)
    s = s:gsub("%[ICON_[%w_]+%]", "")
    s = s:gsub("%[NEWLINE%]", " ")
    s = s:gsub("%[COLOR[%w_]*%]", "")
    s = s:gsub("%[/COLOR%]", "")
    s = s:gsub("|", "/")
    s = s:gsub("%s+", " ")
    return s
end
-- Nil-safe localized lookup: GameInfo rows may have nil Name/Description
-- (e.g. marker traits, or rows lacking a Description column), and
-- Locale.Lookup(nil) raises an error that would kill the whole section.
local function _loc(rawKey)
    if rawKey == nil then return "" end
    return _cleanText(Locale.Lookup(rawKey))
end
"""

# Lua snippet that builds a ``traitUniques`` map: traitType -> list of
# "CATEGORY|name|description" strings for the unique units/buildings/
# districts/improvements granted by that trait. Used by both the diplomacy
# query (per-rival uniques) and the own-abilities query.
_LUA_BUILD_TRAIT_UNIQUES = _LUA_CLEAN_TEXT + """
local traitUniques = {}
local function addUnique(t, kind, name, desc)
    if t == nil then return end
    if traitUniques[t] == nil then traitUniques[t] = {} end
    table.insert(traitUniques[t], kind .. "|" .. name .. "|" .. desc)
end
for u in GameInfo.Units() do addUnique(u.TraitType, "UNIT", _loc(u.Name), _loc(u.Description)) end
for b in GameInfo.Buildings() do addUnique(b.TraitType, "BUILDING", _loc(b.Name), _loc(b.Description)) end
for d in GameInfo.Districts() do addUnique(d.TraitType, "DISTRICT", _loc(d.Name), _loc(d.Description)) end
for imp in GameInfo.Improvements() do addUnique(imp.TraitType, "IMPROVEMENT", _loc(imp.Name), _loc(imp.Description)) end
"""


def build_diplomacy_query() -> str:
    """Rich diplomacy query — runs in InGame context for GetDiplomaticAI access."""
    return """
local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
local pVis = PlayersVisibility[me]
local states = {"ALLIED","DECLARED_FRIEND","FRIENDLY","NEUTRAL","UNFRIENDLY","DENOUNCED","WAR"}
local checkActions = {"DIPLOACTION_DIPLOMATIC_DELEGATION","DIPLOACTION_DECLARE_FRIENDSHIP","DIPLOACTION_DENOUNCE","DIPLOACTION_RESIDENT_EMBASSY","DIPLOACTION_OPEN_BORDERS","DIPLOACTION_MAKE_ALLIANCE"}
{TRAIT_UNIQUES}
for i = 0, 62 do
    if i ~= me and Players[i] and Players[i]:IsAlive() and Players[i]:IsMajor() then
        local cfg = PlayerConfigurations[i]
        local civName = Locale.Lookup(cfg:GetCivilizationShortDescription())
        local leaderName = Locale.Lookup(cfg:GetLeaderName())
        local met = pDiplo:HasMet(i) and "1" or "0"
        local war = pDiplo:IsAtWarWith(i) and "1" or "0"
        if pDiplo:HasMet(i) then
            local ai = Players[i]:GetDiplomaticAI()
            local stateIdx = ai:GetDiplomaticStateIndex(me)
            local stateName = states[stateIdx + 1] or tostring(stateIdx)
            local grievances = pDiplo:GetGrievancesAgainst(i)
            local vis = pDiplo:GetVisibilityOn(i)
            local hasDel = pDiplo:HasDelegationAt(i) and "1" or "0"
            local hasEmb = pDiplo:HasEmbassyAt(i) and "1" or "0"
            local theyDel = Players[i]:GetDiplomacy():HasDelegationAt(me) and "1" or "0"
            local theyEmb = Players[i]:GetDiplomacy():HasEmbassyAt(me) and "1" or "0"
            print("CIV|" .. i .. "|" .. civName .. "|" .. leaderName .. "|" .. met .. "|" .. war .. "|" .. stateName .. "|" .. grievances .. "|" .. vis .. "|" .. hasDel .. "|" .. hasEmb .. "|" .. theyDel .. "|" .. theyEmb)
            local okMil, milStr = pcall(function() return Players[i]:GetStats():GetMilitaryStrength() end)
            local okMyMil, myMilStr = pcall(function() return Players[me]:GetStats():GetMilitaryStrength() end)
            if okMil and okMyMil then print("MILITARY|" .. i .. "|" .. (milStr or 0) .. "|" .. (myMilStr or 0)) end
            local nCivCities = 0
            for _, ec in Players[i]:GetCities():Members() do
                nCivCities = nCivCities + 1
                local ecx, ecy = ec:GetX(), ec:GetY()
                if pVis:IsRevealed(ecx, ecy) then
                    local ecName = Locale.Lookup(ec:GetName())
                    local ecPop = ec:GetPopulation()
                    local ecLoy, ecLoyPT = 100, 0
                    local ecCult = ec:GetCulturalIdentity()
                    if ecCult then ecLoy = ecCult:GetLoyalty(); ecLoyPT = ecCult:GetLoyaltyPerTurn() end
                    local ecWalls, ecDef = 0, 0
                    pcall(function()
                        for _, d in ec:GetDistricts():Members() do
                            local di = GameInfo.Districts[d:GetType()]
                            if di and di.DistrictType == "DISTRICT_CITY_CENTER" then
                                ecWalls = d:GetMaxDamage(DefenseTypes.DISTRICT_OUTER) or 0
                                ecDef = ec:GetStrengthValue() or 0
                                break
                            end
                        end
                    end)
                    print("ECITY|" .. i .. "|" .. ecName:gsub("|","/") .. "|" .. ecx .. "," .. ecy .. "|" .. ecPop .. "|" .. string.format("%.0f|%.1f", ecLoy, ecLoyPT) .. "|" .. ecWalls .. "|" .. ecDef)
                end
            end
            print("CIVCITIES|" .. i .. "|" .. nCivCities)
            local mods = ai:GetDiplomaticModifiers(me)
            if mods then
                for _, mod in ipairs(mods) do
                    local txt = tostring(mod.Text):gsub("|", "/")
                    print("MOD|" .. i .. "|" .. mod.Score .. "|" .. txt)
                end
            end
            if stateIdx == 0 then
                local ok3, aType = pcall(function() return pDiplo:GetAllianceType(i) end)
                if ok3 and aType and aType >= 0 then
                    local aNames = {"RESEARCH","CULTURAL","ECONOMIC","MILITARY","RELIGIOUS"}
                    local aLevel = 1
                    pcall(function() aLevel = pDiplo:GetAllianceLevel(i) or 1 end)
                    print("ALLIANCE|" .. i .. "|" .. (aNames[aType+1] or tostring(aType)) .. "|" .. aLevel)
                end
            end
            local avail = {}
            for _, aName in ipairs(checkActions) do
                local ok2, valid = pcall(function() return pDiplo:IsDiplomaticActionValid(aName, i, false) end)
                if ok2 and valid then
                    local label = aName:gsub("DIPLOACTION_", "")
                    if label == "OPEN_BORDERS" then label = "Open Borders (via propose_trade)" end
                    table.insert(avail, label)
                end
            end
            if not pDiplo:IsAtWarWith(i) then
                local canWar = false
                pcall(function() canWar = pDiplo:CanDeclareWarOn(i) end)
                if canWar then table.insert(avail, "DECLARE_WAR") end
            end
            if #avail > 0 then print("ACTIONS|" .. i .. "|" .. table.concat(avail, ",")) end
            -- Agendas (visibility-gated: historical always, random only at SECRET+)
            local okAg, agendas = pcall(function() return Players[i]:GetAgendaTypes() end)
            if okAg and agendas then
                local histSet = {}
                local leaderType = cfg:GetLeaderTypeName()
                for ha in GameInfo.HistoricalAgendas() do
                    if ha.LeaderType == leaderType then
                        local aDef = GameInfo.Agendas[ha.AgendaType]
                        if aDef then histSet[aDef.Index] = true end
                    end
                end
                local vis = pDiplo:GetVisibilityOn(i)
                for _, agIdx in ipairs(agendas) do
                    local aDef = GameInfo.Agendas[agIdx]
                    if aDef then
                        local isHist = histSet[agIdx] or false
                        if isHist then
                            print("AGENDA|" .. i .. "|HISTORICAL|" .. Locale.Lookup(aDef.Name) .. "|" .. Locale.Lookup(aDef.Description))
                        elseif vis >= 3 then
                            print("AGENDA|" .. i .. "|HIDDEN|" .. Locale.Lookup(aDef.Name) .. "|" .. Locale.Lookup(aDef.Description))
                        else
                            print("AGENDA|" .. i .. "|HIDDEN|???|Requires Secret diplomatic visibility (spy or alliance)")
                        end
                    end
                end
            end
            -- Unique abilities (civ + leader traits) and unique units/buildings/
            -- districts/improvements. Only the named ability traits carry a
            -- Description; marker traits (e.g. infrastructure tags) are filtered
            -- out by requiring a non-empty description.
            local civType = cfg:GetCivilizationTypeName()
            local leaderType = cfg:GetLeaderTypeName()
            local traitSeen = {}
            for ct in GameInfo.CivilizationTraits() do
                if ct.CivilizationType == civType and traitSeen[ct.TraitType] == nil then
                    traitSeen[ct.TraitType] = true
                    local tDef = GameInfo.Traits[ct.TraitType]
                    if tDef and tDef.Description and tDef.Description ~= "" then
                        local tName = _loc(tDef.Name)
                        if tName == "" then tName = ct.TraitType end
                        local tDesc = _loc(tDef.Description)
                        print("TRAIT|" .. i .. "|CIVILIZATION|" .. tName .. "|" .. tDesc)
                    end
                end
            end
            for lt in GameInfo.LeaderTraits() do
                if lt.LeaderType == leaderType and traitSeen[lt.TraitType] == nil then
                    traitSeen[lt.TraitType] = true
                    local tDef = GameInfo.Traits[lt.TraitType]
                    if tDef and tDef.Description and tDef.Description ~= "" then
                        local tName = _loc(tDef.Name)
                        if tName == "" then tName = lt.TraitType end
                        local tDesc = _loc(tDef.Description)
                        print("TRAIT|" .. i .. "|LEADER|" .. tName .. "|" .. tDesc)
                    end
                end
            end
            for t in pairs(traitSeen) do
                local list = traitUniques[t]
                if list then
                    for _, u in ipairs(list) do
                        print("UNIQUE|" .. i .. "|" .. u)
                    end
                end
            end
            local okPact, hasPact = pcall(function() return Players[i]:GetDiplomacy():HasDefensivePact(me) end)
            if okPact and hasPact then print("PACT|" .. i .. "|DEFENSIVE") end
        else
            print("CIV|" .. i .. "|Unmet Civilization|Unknown Leader|" .. met .. "|" .. war .. "|UNKNOWN|0|0|0|0|0|0")
        end
    end
end
-- Scan for third-party defensive pacts
for i = 0, 62 do
    if i ~= me and Players[i] and Players[i]:IsAlive() and Players[i]:IsMajor() and pDiplo:HasMet(i) then
        for j = i+1, 62 do
            if j ~= me and Players[j] and Players[j]:IsAlive() and Players[j]:IsMajor() and pDiplo:HasMet(j) then
                local okP, hp = pcall(function() return Players[i]:GetDiplomacy():HasDefensivePact(j) end)
                if okP and hp then print("PACT|" .. i .. "|" .. j .. "|DEFENSIVE") end
            end
        end
    end
end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL).replace("{TRAIT_UNIQUES}", _LUA_BUILD_TRAIT_UNIQUES)


def build_own_abilities_query() -> str:
    """Resolve the local player's own civ/leader abilities and uniques.

    Emits ``CIV|civName|leaderName``, then ``TRAIT|kind|name|desc`` and
    ``UNIQUE|category|name|desc`` lines. Runs in InGame context (works in
    GameCore too — only uses GameInfo + PlayerConfigurations).
    """
    return """
local me = Game.GetLocalPlayer()
local cfg = PlayerConfigurations[me]
local civName = Locale.Lookup(cfg:GetCivilizationShortDescription())
local leaderName = Locale.Lookup(cfg:GetLeaderName())
print("CIV|" .. civName:gsub("|","/") .. "|" .. leaderName:gsub("|","/"))
{TRAIT_UNIQUES}
local civType = cfg:GetCivilizationTypeName()
local leaderType = cfg:GetLeaderTypeName()
local traitSeen = {}
for ct in GameInfo.CivilizationTraits() do
    if ct.CivilizationType == civType and traitSeen[ct.TraitType] == nil then
        traitSeen[ct.TraitType] = true
        local tDef = GameInfo.Traits[ct.TraitType]
        if tDef and tDef.Description and tDef.Description ~= "" then
            local tName = _loc(tDef.Name)
            if tName == "" then tName = ct.TraitType end
            local tDesc = _loc(tDef.Description)
            print("TRAIT|CIVILIZATION|" .. tName .. "|" .. tDesc)
        end
    end
end
for lt in GameInfo.LeaderTraits() do
    if lt.LeaderType == leaderType and traitSeen[lt.TraitType] == nil then
        traitSeen[lt.TraitType] = true
        local tDef = GameInfo.Traits[lt.TraitType]
        if tDef and tDef.Description and tDef.Description ~= "" then
            local tName = _loc(tDef.Name)
            if tName == "" then tName = lt.TraitType end
            local tDesc = _loc(tDef.Description)
            print("TRAIT|LEADER|" .. tName .. "|" .. tDesc)
        end
    end
end
for t in pairs(traitSeen) do
    local list = traitUniques[t]
    if list then
        for _, u in ipairs(list) do
            print("UNIQUE|" .. u)
        end
    end
end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL).replace("{TRAIT_UNIQUES}", _LUA_BUILD_TRAIT_UNIQUES)


def build_diplomacy_session_query() -> str:
    """Scan for open diplomacy sessions targeting the local player (InGame).

    Emits one minimal line per session — ``SESSION|<pid>|<civ>|<leader>`` —
    the only consumer being end_turn's auto-resolve, which closes sessions
    silently.  No relationship/war flag: it cannot distinguish "war
    declared" from "peace offer from a wartime rival" — war and denounce
    *events* come from the diplomatic-state watch
    (:func:`build_diplo_state_watch_query`) instead.  Must run in the
    InGame context — ``DiplomacyManager`` is nil in GameCore.
    """
    return f"""
local me = Game.GetLocalPlayer()
local found = false
for i = 0, 62 do
    if i ~= me and Players[i] and Players[i]:IsAlive() then
        local sid = DiplomacyManager.FindOpenSessionID(me, i)
        if sid and sid >= 0 then
            local cfg = PlayerConfigurations[i]
            local civName = Locale.Lookup(cfg:GetCivilizationShortDescription())
            local leaderName = Locale.Lookup(cfg:GetLeaderName())
            print("SESSION|" .. i .. "|" .. civName:gsub("|","/") .. "|" .. leaderName:gsub("|","/"))
            found = true
        end
    end
end
if not found then print("NONE") end
print("{SENTINEL}")
"""


def build_diplo_state_watch_query() -> str:
    """One line per met major civ: their diplomatic state toward the local
    player (InGame context — GetDiplomaticAI access).

    ``DIPLO_STATE|<pid>|<civ>|<leader>|<STATE>`` where STATE is the same
    mapping as build_diplomacy_query (ALLIED, DECLARED_FRIEND, FRIENDLY,
    NEUTRAL, UNFRIENDLY, DENOUNCED, WAR).  Consumed by end_turn's
    diplomatic-state watch: transitions into WAR or DENOUNCED are reported
    to the agent as important, response-free information — regardless of
    whether a session accompanied them, since AI war declarations and
    denunciations also land while the agent is off the clock with no
    session at all.
    """
    return f"""
local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
local states = {{"ALLIED","DECLARED_FRIEND","FRIENDLY","NEUTRAL","UNFRIENDLY","DENOUNCED","WAR"}}
for i = 0, 62 do
    if i ~= me and Players[i] and Players[i]:IsAlive() and Players[i]:IsMajor() and pDiplo:HasMet(i) then
        local okS, st = pcall(function()
            return Players[i]:GetDiplomaticAI():GetDiplomaticStateIndex(me)
        end)
        if okS and st then
            local cfg = PlayerConfigurations[i]
            local civName = Locale.Lookup(cfg:GetCivilizationShortDescription())
            local leaderName = Locale.Lookup(cfg:GetLeaderName())
            print("DIPLO_STATE|" .. i .. "|" .. civName:gsub("|","/") .. "|"
                .. leaderName:gsub("|","/") .. "|" .. (states[st + 1] or tostring(st)))
        end
    end
end
print("{SENTINEL}")
"""


def build_diplomacy_choices_query(other_player_id: int) -> str:
    """Get available dialogue choices for an open session with a specific player."""
    return f"""
local me = Game.GetLocalPlayer()
local sid = DiplomacyManager.FindOpenSessionID(me, {other_player_id})
if sid == nil or sid < 0 then {_bail("ERR:NO_SESSION")} end
print("SESSION|" .. sid)
local ctrl = ContextPtr:LookUpControl("/InGame/DiplomacyActionView")
local isVisible = ctrl and not ctrl:IsHidden() or false
print("VISIBLE|" .. tostring(isVisible))
for row in GameInfo.DiplomacySelections() do
    if string.find(row.Type, "FIRST_MEET") or string.find(row.Type, "GREETING") or string.find(row.Type, "DECLARE_FRIEND") or string.find(row.Type, "DENOUNCE") then
        local text = Locale.Lookup(row.Text)
        print("CHOICE|" .. row.Type .. "|" .. row.Key .. "|" .. text)
    end
end
print("{SENTINEL}")
"""


def build_diplomacy_respond(other_player_id: int, response: str) -> str:
    """Respond to a diplomacy session.

    response is 'POSITIVE', 'NEGATIVE', or 'EXIT'.
    EXIT closes the session directly (last-resort for orphaned sessions).
    POSITIVE/NEGATIVE sends AddResponse only — does NOT call CloseSession.
    The C++ engine handles session lifecycle through its own callbacks.
    Caller must check session state in a SEPARATE call to allow the engine
    time to process the response (same-frame checks see stale state).

    View dismissal is deliberately NOT part of this builder: raw hide
    events (ShowIngameUI/HideLeaderScreen) skip the DiplomacyActionView's
    teardown and unbalance the engine's bulk-hide bookkeeping (live-observed
    frozen UI).  If the session popped the leader screen, the caller
    schedules the delayed DAV-context Close() instead (see
    GameState._cleanup_diplo_screen).
    """
    return f"""
local me = Game.GetLocalPlayer()
local sid = DiplomacyManager.FindOpenSessionID(me, {other_player_id})
if sid == nil or sid < 0 then {_bail("ERR:NO_SESSION")} end
if "{response}" == "EXIT" then
    DiplomacyManager.CloseSession(sid)
    print("OK:SESSION_CLOSED")
    print("{SENTINEL}"); return
end
DiplomacyManager.AddResponse(sid, me, "{response}")
print("OK:RESPONSE_SENT|{response}")
print("{SENTINEL}")
"""


def _diplo_action_validity_lua(acting: str = "me") -> str:
    """Lua snippet: validate ``DIPLOACTION_<action>`` against ``target`` and
    bail with ``ERR:INVALID|<reasons>`` when it fails.

    Expects the locals ``me``, ``target``, ``action`` and ``pDiplo`` to be
    defined in scope.  ``acting`` is the Lua expression for the acting
    player id — the local player by default (``me``), but the accept-time
    recipe passes the *proposer's* id because the proposer's diplomacy
    object owns the action's validity while the target is the local player.
    Shared by :func:`build_send_diplo_action` (pre-check before opening a
    session) and :func:`build_check_diplo_action_validity` (standalone gate
    for mailbox filing).
    """
    return f"""local fullAction = "DIPLOACTION_" .. action
local valid, results = pDiplo:IsDiplomaticActionValid(fullAction, target, true)
if not valid then
    local reasons = "unknown"
    if results and results.FailureReasons then
        local parts = {{}}
        for _, r in ipairs(results.FailureReasons) do
            local s = tostring(r or "")
            if s:find("OBSOLETE_CIVIC") or s:find("ObsoleteCivic") then
                table.insert(parts, "obsolete (Diplomatic Service civic researched — use embassy instead)")
            else
                local loc = Locale.Lookup(s)
                if loc and loc ~= "" then table.insert(parts, loc) else table.insert(parts, s) end
            end
        end
        if #parts > 0 then reasons = table.concat(parts, "; ") end
    end
    if reasons == "unknown" and action == "DIPLOMATIC_DELEGATION" then
        local dipSvcCivic = GameInfo.Civics["CIVIC_DIPLOMATIC_SERVICE"]
        if dipSvcCivic then
            local hasCivic = false
            pcall(function() hasCivic = Players[{acting}]:GetCulture():HasCivic(dipSvcCivic.Index) end)
            if hasCivic then
                reasons = "obsolete (Diplomatic Service civic researched — use embassy instead)"
            end
        end
    end
    {_bail_lua('"ERR:INVALID|" .. reasons')}
end"""


def build_check_diplo_action_validity(
    other_player_id: int,
    action_name: str,
    acting_player_id: int | None = None,
) -> str:
    """Pure validity check for a response-able diplo action (InGame context).

    Prints ``OK|<action>`` when ``IsDiplomaticActionValid`` passes, otherwise
    bails with ``ERR:INVALID|<reasons>``.  Does NOT open a session or change
    any state — used to gate mailbox filing of agent→human proposals so the
    human is never asked to answer a doomed proposal (already friends,
    delegation obsolete after Diplomatic Service, missing capital path, ...).

    ``acting_player_id`` overrides whose diplomacy object is queried (the
    proposer's for the accept-time recipe, which runs while the *target* is
    the local player); it defaults to the local player.
    """
    me_expr = (
        "Game.GetLocalPlayer()"
        if acting_player_id is None
        else str(int(acting_player_id))
    )
    return f"""
local me = {me_expr}
local pDiplo = Players[me]:GetDiplomacy()
local target = {other_player_id}
local action = "{action_name}"
{_diplo_action_validity_lua()}
print("OK|" .. action)
print("{SENTINEL}")
"""


def build_send_diplo_action(other_player_id: int, action_name: str) -> str:
    """Send a proactive diplomatic action and detect acceptance/rejection.

    action_name is e.g. DENOUNCE, DECLARE_SURPRISE_WAR, DECLARE_FORMAL_WAR,
    etc. — **one-way actions only**. The three response-able actions
    (DECLARE_FRIENDSHIP, DIPLOMATIC_DELEGATION, RESIDENT_EMBASSY) are
    refused outright: this builder's proposer-side ``AddResponse`` flow is
    silently ignored by the engine (live-verified — its OK:ACCEPTED print
    was a false positive; see DIPLO_EXECUTION_PLAN.md §1). Accepted
    proposals to managed civs are completed target-local instead via the
    recipe builders below, at accept time on the target's turn.

    Open Borders is NOT supported here — it's a trade deal, not a diplomatic
    action. Use propose_trade with AGREEMENT/OPEN_BORDERS items instead.

    The Lua lives in ``build_send_diplo_action.lua`` (loaded via
    ``load_lua_template``); see its header for the RequestSession string
    quirks and the flow notes. War declarations (DECLARE_*_WAR) leave the
    session open so the leader animation plays — Python schedules cleanup
    afterwards (``_cleanup_war_diplomacy`` in game_state).
    """
    if action_name in RESPONSEABLE_DIPLO_ACTIONS:
        return (
            f'print("ERR:NOT_SUPPORTED|{action_name} is a response-able '
            "action: the proposer-side AddResponse flow is silently ignored "
            "by the engine. Accepted proposals to managed civs are executed "
            'at accept time on the target\'s turn (respond_to_diplo_action '
            'completes them); pure-AI targets cannot be proposed to from '
            'this path.") '
            f'print("{SENTINEL}")'
        )
    # Map action_name to the correct RequestSession string
    # Game source: DiplomacyActionView.lua line 472 uses "DECLARE_FRIEND"
    is_war = action_name.endswith("_WAR") and action_name.startswith("DECLARE_")
    session_str = DIPLO_SESSION_STRING_MAP.get(action_name, action_name)
    return (
        load_lua_template("build_send_diplo_action.lua")
        .replace("__MCP_TARGET_TAG__", str(other_player_id))
        .replace("__MCP_ACTION_TAG__", action_name)
        .replace("__MCP_SESSION_STRING_TAG__", session_str)
        .replace("__MCP_IS_WAR_TAG__", "true" if is_war else "false")
        .replace("__MCP_VALIDITY_BLOCK_TAG__", _diplo_action_validity_lua())
        .replace("__MCP_SENTINEL_TAG__", SENTINEL)
    )


# ---------------------------------------------------------------------------
# Accept-time execution recipe (DIPLO_EXECUTION_PLAN.md §3). The individual
# engine steps as Lua builders; the orchestration (round-trips, delays,
# adoption polls) lives in server._execute_diplo_agreement because the engine
# needs real frames between steps. Runs while the TARGET is the local player.
# ---------------------------------------------------------------------------


def build_diplo_open_step(from_player: int, to_player: int, session_str: str) -> str:
    """Recipe steps 2+3 (InGame ctx): bare-close a stale session for this
    pair, then ``RequestSession(from, to, str)``.

    Prints ``OK|OPENED|<sid>`` — the sid is found via
    ``FindOpenSessionID(to, from)`` because the target is the responder —
    or bails with ``ERR:...``.
    """
    return f"""
local from = {from_player}
local to = {to_player}
local stale = DiplomacyManager.FindOpenSessionID(to, from)
if stale and stale >= 0 then
    DiplomacyManager.CloseSession(stale)
    print("MCP_TRACE|diplo_open: closed stale session " .. tostring(stale))
end
local okRS, errRS = pcall(function()
    DiplomacyManager.RequestSession(from, to, "{session_str}")
end)
if not okRS then {_bail_lua('"ERR:REQUEST_FAILED|" .. tostring(errRS)')} end
local sid = DiplomacyManager.FindOpenSessionID(to, from)
if not sid or sid < 0 then {_bail("ERR:NO_SESSION|RequestSession opened no session")} end
print("OK|OPENED|" .. sid)
print("{SENTINEL}")
"""


def build_diplo_prime_step(from_player: int, to_player: int, enum_key: str) -> str:
    """Recipe step 4 (InGame ctx): ``SendAction(from, to, DiplomacyActionTypes.<enum>, {})``.

    The prime applies nothing by itself (live-verified — ``TestAction``
    returns false even for valid actions, and SendAction alone registers no
    effect); it exists so the DiplomacyActionView adopts the session once
    nudged. Prints ``OK|PRIMED|<ret>|sid=<sid>`` or bails with ``ERR:...``.
    """
    return f"""
local from = {from_player}
local to = {to_player}
local sid = DiplomacyManager.FindOpenSessionID(to, from)
if not sid or sid < 0 then {_bail("ERR:NO_SESSION|no open session to prime")} end
local okSA, resSA = pcall(function()
    return DiplomacyManager.SendAction(from, to, DiplomacyActionTypes.{enum_key}, {{}})
end)
if not okSA then {_bail_lua('"ERR:PRIME_FAILED|" .. tostring(resSA)')} end
print("OK|PRIMED|" .. tostring(resSA) .. "|sid=" .. sid)
print("{SENTINEL}")
"""


def build_diplo_response_step(sid: int, to_player: int) -> str:
    """Recipe steps 5+7 (DiplomacyActionView ctx): ``AddResponse(sid, to,
    "POSITIVE")`` from the target.

    The first call is the adoption nudge — it does NOT complete the orphan
    session (live-verified), it triggers the statement delivery the view
    adopts. Once ``ms_ActiveSessionID == sid``, the second call completes
    the proposal. Prints ``OK|RESPONSE_SENT|<sid>`` or
    ``ERR:RESPONSE_FAILED|...``.
    """
    return f"""
local okAR, errAR = pcall(function()
    DiplomacyManager.AddResponse({sid}, {to_player}, "POSITIVE")
end)
if not okAR then {_bail_lua('"ERR:RESPONSE_FAILED|" .. tostring(errAR)')} end
print("OK|RESPONSE_SENT|{sid}")
print("{SENTINEL}")
"""


def build_diplo_adoption_check(sid: int) -> str:
    """Recipe step 6 (DiplomacyActionView ctx): has the view adopted the
    session yet?

    Prints ``ADOPTED|true``/``ADOPTED|false`` by comparing the file-scope
    global ``ms_ActiveSessionID`` to the expected sid. ``ms_Mode`` may stay
    nil even after adoption (observed live) — never gate on it.
    """
    return f"""
print("ADOPTED|" .. tostring(ms_ActiveSessionID == {sid}))
pcall(function()
    print("MCP_TRACE|adoption: ms_ActiveSessionID=" .. tostring(ms_ActiveSessionID)
        .. " ms_Mode=" .. tostring(ms_Mode))
end)
print("{SENTINEL}")
"""


def build_diplo_effect_check(from_player: int, to_player: int, action_name: str) -> str:
    """Recipe step 8 (InGame ctx, separate round-trip — same-frame reads are
    stale): did the effect register?

    Prints ``VALID|<bool>`` (the step-1 oracle re-read: valid-before +
    invalid-after means applied), ``HAS_DELEGATION|<bool>`` (delegations
    only — ``Players[from]:GetDiplomacy():HasDelegationAt(to)`` is the
    direction the action creates) and ``STATE|<idx>|<idx>`` (diagnostics:
    both diplomatic state indices flip to 1/FRIENDS on friendship).
    """
    return f"""
local from = {from_player}
local to = {to_player}
local action = "{action_name}"
local valid = true
pcall(function()
    valid = Players[from]:GetDiplomacy():IsDiplomaticActionValid(
        "DIPLOACTION_" .. action, to, true)
end)
print("VALID|" .. tostring(valid))
if action == "DIPLOMATIC_DELEGATION" then
    local hasDel = false
    pcall(function()
        hasDel = Players[from]:GetDiplomacy():HasDelegationAt(to)
    end)
    print("HAS_DELEGATION|" .. tostring(hasDel))
end
local sFrom, sTo = -1, -1
pcall(function() sFrom = Players[from]:GetDiplomaticAI():GetDiplomaticStateIndex(to) end)
pcall(function() sTo = Players[to]:GetDiplomaticAI():GetDiplomaticStateIndex(from) end)
print("STATE|" .. sFrom .. "|" .. sTo)
print("{SENTINEL}")
"""


def build_diplo_close_step(from_player: int, to_player: int) -> str:
    """Recipe step 9 teardown (InGame ctx): bare ``CloseSession`` of any open
    session for this pair — no hide events, no responses.

    Closing + firing hide events in the same instant leaves the view with a
    stale ``ms_ActiveSessionID`` that swallows the next session's statement
    event (live-observed); the leader-screen dismiss is a separate, delayed
    round-trip (``handoff.build_dismiss_leader_screen_lua``).
    """
    return f"""
local sid = DiplomacyManager.FindOpenSessionID({to_player}, {from_player})
if sid and sid >= 0 then
    DiplomacyManager.CloseSession(sid)
    print("OK|CLOSED|" .. sid)
else
    print("OK|NO_OPEN_SESSION")
end
print("{SENTINEL}")
"""


def build_war_close_session(other_player_id: int) -> str:
    """Phase 1: close the war diplomacy session (InGame context).

    After this, DiplomacyActionView transitions to OVERVIEW_MODE (intel screen).
    Must wait ~1s for the engine event to process before phase 2 — which is
    now the DAV-context Close() (``handoff.build_dismiss_leader_screen_lua``);
    the old NaturalWonderPopup trick could not release a frozen view.
    """
    sentinel = SENTINEL
    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local sid = DiplomacyManager.FindOpenSessionID(me, target)
if sid and sid >= 0 then
    DiplomacyManager.CloseSession(sid)
end
print("OK:SESSION_CLOSED")
print("{sentinel}")
"""


def build_deal_options_query(other_player_id: int) -> str:
    """Show what both sides can trade — resources, gold, favor, agreements (InGame)."""
    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local pDiplo = Players[me]:GetDiplomacy()
if not Players[target] or not Players[target]:IsAlive() then {_bail(f"ERR:INVALID_PLAYER|Player {other_player_id} not found")} end
if not pDiplo:HasMet(target) then {_bail(f"ERR:NOT_MET|Have not met player {other_player_id}")} end
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
print("CIV|" .. target .. "|" .. name:gsub("|","/"))
local ourGold = math.floor(Players[me]:GetTreasury():GetGoldBalance())
local ourGPT = math.floor(Players[me]:GetTreasury():GetGoldYield() - Players[me]:GetTreasury():GetTotalMaintenance())
local ourFavor = 0
pcall(function() ourFavor = math.floor(Players[me]:GetFavor() or 0) end)
local theirGold = math.floor(Players[target]:GetTreasury():GetGoldBalance())
local theirGPT = math.floor(Players[target]:GetTreasury():GetGoldYield() - Players[target]:GetTreasury():GetTotalMaintenance())
local theirFavor = 0
pcall(function() theirFavor = math.floor(Players[target]:GetFavor() or 0) end)
print("ECON|" .. ourGold .. "|" .. ourGPT .. "|" .. ourFavor .. "|" .. theirGold .. "|" .. theirGPT .. "|" .. theirFavor)
for row in GameInfo.Resources() do
    local ourAmt = Players[me]:GetResources():GetResourceAmount(row.Index)
    local theirAmt = Players[target]:GetResources():GetResourceAmount(row.Index)
    if ourAmt > 0 or theirAmt > 0 then
        local rClass = row.ResourceClassType or ""
        local rName = Locale.Lookup(row.Name)
        print("RES|" .. rName:gsub("|","/") .. "|" .. row.ResourceType .. "|" .. rClass .. "|" .. ourAmt .. "|" .. theirAmt)
    end
end
local hasOB = false
pcall(function() hasOB = pDiplo:HasOpenBordersFrom(target) end)
if not hasOB then pcall(function() hasOB = pDiplo:GetVisibilityOn(target) >= 2 end) end
print("OB|" .. (hasOB and "1" or "0"))
local ai = Players[target]:GetDiplomaticAI()
local stateIdx = ai:GetDiplomaticStateIndex(me)
local hasDiploService = false
pcall(function()
    local civic = GameInfo.Civics["CIVIC_DIPLOMATIC_SERVICE"]
    if civic then hasDiploService = Players[me]:GetCulture():HasCivic(civic.Index) end
end)
local allianceEligible = (stateIdx == 1 and hasDiploService)
local currentAlliance = ""
if stateIdx == 0 then
    local ok3, aType = pcall(function() return pDiplo:GetAllianceType(target) end)
    if ok3 and aType and aType >= 0 then
        local aNames = {{"RESEARCH","CULTURAL","ECONOMIC","MILITARY","RELIGIOUS"}}
        currentAlliance = aNames[aType+1] or ""
    end
end
print("ALLIANCE|" .. (allianceEligible and "1" or "0") .. "|" .. currentAlliance)
for _, city in Players[me]:GetCities():Members() do
    local cName = Locale.Lookup(city:GetName()):gsub("|", "/")
    local cid = city:GetID()
    local pop = city:GetPopulation()
    local isCapital = city:IsCapital() and "1" or "0"
    print("CITY|OURS|" .. cid .. "|" .. cName .. "|" .. pop .. "|" .. isCapital)
end
for _, city in Players[target]:GetCities():Members() do
    local cName = Locale.Lookup(city:GetName()):gsub("|", "/")
    local cid = city:GetID()
    local pop = city:GetPopulation()
    local isCapital = city:IsCapital() and "1" or "0"
    print("CITY|THEIRS|" .. cid .. "|" .. cName .. "|" .. pop .. "|" .. isCapital)
end
print("{SENTINEL}")
"""


def parse_deal_options_response(lines: list[str]) -> DealOptions:
    """Parse the deal options query response."""
    opts = DealOptions(other_player_id=0, other_civ_name="")
    for line in lines:
        if line.startswith("CIV|"):
            parts = line.split("|")
            if len(parts) >= 3:
                opts.other_player_id = int(parts[1])
                opts.other_civ_name = parts[2]
        elif line.startswith("ECON|"):
            parts = line.split("|")
            if len(parts) >= 7:
                opts.our_gold = int(parts[1])
                opts.our_gpt = int(parts[2])
                opts.our_favor = int(parts[3])
                opts.their_gold = int(parts[4])
                opts.their_gpt = int(parts[5])
                opts.their_favor = int(parts[6])
        elif line.startswith("RES|"):
            parts = line.split("|")
            if len(parts) >= 6:
                name = parts[1]
                res_type = parts[2]
                res_class = parts[3]
                our_amt = int(parts[4])
                their_amt = int(parts[5])
                is_luxury = "LUXURY" in res_class
                is_strategic = "STRATEGIC" in res_class
                if our_amt > 0:
                    label = f"{name} x{our_amt}" if our_amt > 1 else name
                    if is_luxury:
                        opts.our_luxuries.append(label)
                    elif is_strategic:
                        opts.our_strategics.append(label)
                if their_amt > 0:
                    label = f"{name} x{their_amt}" if their_amt > 1 else name
                    if is_luxury:
                        opts.their_luxuries.append(label)
                    elif is_strategic:
                        opts.their_strategics.append(label)
        elif line.startswith("OB|"):
            opts.has_open_borders = line.split("|")[1] == "1"
        elif line.startswith("ALLIANCE|"):
            parts = line.split("|")
            if len(parts) >= 3:
                opts.alliance_eligible = parts[1] == "1"
                if parts[2]:
                    opts.current_alliance = parts[2]
        elif line.startswith("CITY|"):
            parts = line.split("|")
            if len(parts) >= 6:
                city = TradeableCity(
                    city_id=int(parts[2]),
                    name=parts[3],
                    population=int(parts[4]),
                    is_capital=parts[5] == "1",
                )
                if parts[1] == "OURS":
                    opts.our_cities.append(city)
                else:
                    opts.their_cities.append(city)
    return opts


def build_pending_deals_query() -> str:
    """Scan all met players for incoming trade deal offers (InGame context)."""
    return """
local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
for i = 0, 62 do
    if i ~= me and Players[i] and Players[i]:IsAlive() and Players[i]:IsMajor() and pDiplo:HasMet(i) then
        local sid = DiplomacyManager.FindOpenSessionID(me, i)
        if sid and sid >= 0 then
        local ok, deal = pcall(function() return DealManager.GetWorkingDeal(DealDirection.INCOMING, me, i) end)
        if ok and deal then
            local count = deal:GetItemCount()
            if count and count > 0 then
                local cfg = PlayerConfigurations[i]
                local civName = Locale.Lookup(cfg:GetCivilizationShortDescription())
                local leaderName = Locale.Lookup(cfg:GetLeaderName())
                print("DEAL|" .. i .. "|" .. civName:gsub("|","/") .. "|" .. leaderName:gsub("|","/"))
                for item in deal:Items() do
                    local fromID = item:GetFromPlayerID()
                    local iType = item:GetType()
                    local subType = item:GetSubType()
                    local amount = item:GetAmount() or 0
                    local duration = item:GetDuration() or 0
                    local valueType = item:GetValueType() or -1
                    local typeName = "UNKNOWN"
                    local itemName = "Unknown"
                    if iType == DealItemTypes.GOLD then
                        typeName = "GOLD"
                        if duration > 0 then itemName = "Gold per turn" else itemName = "Gold (lump sum)" end
                    elseif iType == DealItemTypes.RESOURCES then
                        typeName = "RESOURCE"
                        local res = GameInfo.Resources[valueType]
                        if res then itemName = Locale.Lookup(res.Name) else itemName = "Resource#" .. tostring(valueType) end
                    elseif iType == DealItemTypes.AGREEMENTS then
                        typeName = "AGREEMENT"
                        if subType == DealAgreementTypes.OPEN_BORDERS then itemName = "Open Borders"
                        elseif subType == DealAgreementTypes.JOINT_WAR then 
                            itemName = "Joint War: " .. valueType
                        elseif subType == DealAgreementTypes.ALLIANCE then
                            local aNames = {"Research","Cultural","Economic","Military","Religious"}
                            itemName = (valueType >= 0 and valueType < 5 and aNames[valueType+1] or "Unknown") .. " Alliance"
                        else itemName = "" end
                    elseif iType == DealItemTypes.FAVOR then
                        typeName = "FAVOR"
                        itemName = "Diplomatic Favor"
                    elseif iType == DealItemTypes.CITIES then
                        typeName = "CITY"
                        itemName = "City"
                    elseif iType == DealItemTypes.GREATWORK then
                        typeName = "GREAT_WORK"
                        itemName = "Great Work"
                    end
                    if itemName ~= "" and itemName ~= "Unknown" then
                        local fromTag = "THEM"
                        if fromID == me then fromTag = "US" end
                        print("ITEM|" .. i .. "|" .. fromTag .. "|" .. typeName .. "|" .. itemName:gsub("|","/") .. "|" .. amount .. "|" .. duration)
                    end
                end
            end
        end
        end
    end
end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_respond_to_deal(other_player_id: int, accept: bool) -> str:
    """Accept or reject a pending trade deal (InGame context)."""
    action = "DealProposalAction.ACCEPTED" if accept else "DealProposalAction.REJECTED"
    verb = "ACCEPTED" if accept else "REJECTED"
    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local sid = DiplomacyManager.FindOpenSessionID(me, target)
if not sid or sid < 0 then {_bail(f"ERR:NO_DEAL|No active deal session with player {other_player_id}")} end
DealManager.SendWorkingDeal({action}, me, target)
{_lua_close_diplo_session()}
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
print("OK:DEAL_{verb}|" .. name)
print("{SENTINEL}")
"""


def _lua_deal_item(from_var: str, item: dict) -> str:
    """Generate Lua snippet to add one item to the working deal.

    from_var: Lua variable name for the player ID (e.g. "me" or "target").
    item: dict with keys type, amount, and optionally name, duration.
    """
    t = item["type"].upper()
    amount = item.get("amount", 0)
    duration = item.get("duration", 0)

    if t == "GOLD":
        return (
            f"do local gi = deal:AddItemOfType(DealItemTypes.GOLD, {from_var}) "
            f"if gi then gi:SetAmount({amount}) gi:SetDuration({duration}) end end"
        )
    elif t == "RESOURCE":
        res_name = item["name"]
        res_amount = item.get("amount", 1)
        res_duration = item.get("duration", 30)
        return (
            f'do local res = GameInfo.Resources["{res_name}"] '
            f"if res then local ri = deal:AddItemOfType(DealItemTypes.RESOURCES, {from_var}) "
            f"if ri then ri:SetValueType(res.Index) ri:SetAmount({res_amount}) "
            f"ri:SetDuration({res_duration}) end end end"
        )
    elif t == "FAVOR":
        return (
            f"do local fi = deal:AddItemOfType(DealItemTypes.FAVOR, {from_var}) "
            f"if fi then fi:SetAmount({amount}) end end"
        )
    elif t == "AGREEMENT":
        subtype = item["subtype"]  # "OPEN_BORDERS", "JOINT_WAR", "ALLIANCE"
        return (
            f"do local ai = deal:AddItemOfType(DealItemTypes.AGREEMENTS, {from_var}) "
            f"if ai then ai:SetSubType(DealAgreementTypes.{subtype}) end end"
        )
    elif t == "CITY":
        city_id = item["city_id"]
        return (
            f"do local ci = deal:AddItemOfType(DealItemTypes.CITIES, {from_var}) "
            f"if ci then ci:SetValueType({city_id}) end end"
        )
    else:
        return f"-- unsupported deal item type: {t}"


def build_propose_trade(
    other_player_id: int,
    offer_items: list[dict],
    request_items: list[dict],
) -> str:
    """Build a trade deal proposal and send it (InGame context).

    offer_items: items we give to them (from us).
    request_items: items we want from them.
    Each item dict: {type: GOLD|RESOURCE|FAVOR|AGREEMENT|CITY, amount: int, name: str, duration: int, subtype: str, city_id: int}
    """
    offer_lua = " ".join(_lua_deal_item("me", item) for item in offer_items)
    request_lua = " ".join(_lua_deal_item("target", item) for item in request_items)

    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local pDiplo = Players[me]:GetDiplomacy()
if not pDiplo:HasMet(target) then {_bail("ERR:NOT_MET|Have not met player " + str(other_player_id))} end
if pDiplo:IsAtWarWith(target) then {_bail("ERR:AT_WAR|Cannot trade while at war")} end
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
-- Always clear: the working deal persists across calls, and a leftover deal
-- (e.g. from a prior test_trade EQUALIZE) would stack its items underneath
-- ours and silently double the amounts actually traded.
DealManager.ClearWorkingDeal(DealDirection.OUTGOING, me, target)
local deal = DealManager.GetWorkingDeal(DealDirection.OUTGOING, me, target)
if not deal then {_bail("ERR:NO_DEAL_OBJECT|Failed to get working deal")} end
{offer_lua}
{request_lua}
DiplomacyManager.RequestSession(me, target, "MAKE_DEAL")
DealManager.SendWorkingDeal(DealProposalAction.PROPOSED, me, target)
local sid = DiplomacyManager.FindOpenSessionID(me, target)
local result = "PROPOSED"
local termsStr = ""
if sid and sid >= 0 then
    local ok, respDeal = pcall(function()
        return DealManager.GetWorkingDeal(DealDirection.INCOMING, me, target)
    end)
    if ok and respDeal and respDeal:GetItemCount() and respDeal:GetItemCount() > 0 then
        DealManager.SendWorkingDeal(DealProposalAction.ACCEPTED, me, target)
        result = "ACCEPTED"
        -- Report the actual deal terms (AI may have counter-offered different terms)
        local weGive = {{}}
        local theyGive = {{}}
        for item in respDeal:Items() do
            local fromID = item:GetFromPlayerID()
            local iType = item:GetType()
            local subType = item:GetSubType() or -1
            local amount = item:GetAmount() or 0
            local duration = item:GetDuration() or 0
            local desc = "Unknown"
            if iType == DealItemTypes.GOLD then
                if duration > 0 then desc = amount .. " gold/turn (" .. duration .. " turns)"
                else desc = amount .. " gold" end
            elseif iType == DealItemTypes.AGREEMENTS then
                if subType == DealAgreementTypes.OPEN_BORDERS then desc = "Open Borders"
                elseif subType == DealAgreementTypes.JOINT_WAR then desc = "Joint War"
                else desc = "Agreement" end
            elseif iType == DealItemTypes.RESOURCES then
                local res = GameInfo.Resources[item:GetValueType() or -1]
                local rName = res and Locale.Lookup(res.Name) or "Resource"
                if duration > 0 then desc = amount .. "x " .. rName .. " (" .. duration .. "t)"
                else desc = amount .. "x " .. rName end
            elseif iType == DealItemTypes.FAVOR then
                desc = amount .. " Diplomatic Favor"
            elseif iType == DealItemTypes.CITIES then
                desc = "City"
            end
            if fromID == me then table.insert(weGive, desc)
            else table.insert(theyGive, desc) end
        end
        if #weGive > 0 or #theyGive > 0 then
            termsStr = "\\nActual deal terms:"
            if #weGive > 0 then termsStr = termsStr .. "\\n  We give: " .. table.concat(weGive, ", ") end
            if #theyGive > 0 then termsStr = termsStr .. "\\n  They give: " .. table.concat(theyGive, ", ") end
        end
    else
        result = "REJECTED"
    end
    {_lua_close_diplo_session()}
end
print("OK:" .. result .. "|Trade " .. result:lower() .. " with " .. name .. termsStr)
print("{SENTINEL}")
"""


def build_test_trade(
    other_player_id: int,
    offer_items: list[dict],
    request_items: list[dict],
) -> str:
    """Test a trade deal via EQUALIZE — returns what the AI thinks is fair (InGame).

    Same item format as build_propose_trade. Does NOT commit the deal.
    """
    offer_lua = " ".join(_lua_deal_item("me", item) for item in offer_items)
    request_lua = " ".join(_lua_deal_item("target", item) for item in request_items)

    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local pDiplo = Players[me]:GetDiplomacy()
if not pDiplo:HasMet(target) then {_bail("ERR:NOT_MET|Have not met player " + str(other_player_id))} end
if pDiplo:IsAtWarWith(target) then {_bail("ERR:AT_WAR|Cannot trade while at war")} end
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
print("CIV|" .. target .. "|" .. name:gsub("|","/"))
pcall(function()
    local sid = DiplomacyManager.FindOpenSessionID(me, target)
    if sid and sid >= 0 then DiplomacyManager.CloseSession(sid) end
end)
DiplomacyManager.RequestSession(me, target, "MAKE_DEAL")
DealManager.ClearWorkingDeal(DealDirection.OUTGOING, me, target)
local deal = DealManager.GetWorkingDeal(DealDirection.OUTGOING, me, target)
if not deal then {_bail("ERR:NO_DEAL_OBJECT|Failed to get working deal")} end
{offer_lua}
{request_lua}
print("PROPOSED_ITEMS")
for item in deal:Items() do
    local fromTag = item:GetFromPlayerID() == me and "US" or "THEM"
    local itype = item:GetType()
    local typeName = "UNKNOWN"
    local ok3, vid = pcall(function() return item:GetValueTypeID() end); vid = (ok3 and vid) and tostring(vid) or ""
    local ok4, sid2 = pcall(function() return item:GetSubTypeID() end); sid2 = (ok4 and sid2) and tostring(sid2) or ""
    local amount = item:GetAmount() or 0
    local duration = item:GetDuration() or 0
    if itype == DealItemTypes.GOLD then typeName = "GOLD"
    elseif itype == DealItemTypes.RESOURCES then typeName = "RESOURCE"
    elseif itype == DealItemTypes.AGREEMENTS then typeName = "AGREEMENT"
    elseif itype == DealItemTypes.FAVOR then typeName = "FAVOR"
    elseif itype == DealItemTypes.CITIES then typeName = "CITY"
    elseif itype == DealItemTypes.GREATWORK then typeName = "GREAT_WORK"
    end
    print("ITEM|" .. fromTag .. "|" .. typeName .. "|" .. amount .. "|" .. duration .. "|" .. vid .. "|" .. sid2)
end
DealManager.SendWorkingDeal(DealProposalAction.EQUALIZE, me, target)
local inDeal = DealManager.GetWorkingDeal(DealDirection.INCOMING, me, target)
if inDeal and inDeal:GetItemCount() and inDeal:GetItemCount() > 0 then
    print("AI_COUNTER")
    for item in inDeal:Items() do
        local fromTag = item:GetFromPlayerID() == me and "US" or "THEM"
        local itype = item:GetType()
        local typeName = "UNKNOWN"
        local ok3, vid = pcall(function() return item:GetValueTypeID() end); vid = (ok3 and vid) and tostring(vid) or ""
        local ok4, sid2 = pcall(function() return item:GetSubTypeID() end); sid2 = (ok4 and sid2) and tostring(sid2) or ""
        local amount = item:GetAmount() or 0
        local duration = item:GetDuration() or 0
        if itype == DealItemTypes.GOLD then typeName = "GOLD"
        elseif itype == DealItemTypes.RESOURCES then typeName = "RESOURCE"
        elseif itype == DealItemTypes.AGREEMENTS then typeName = "AGREEMENT"
        elseif itype == DealItemTypes.FAVOR then typeName = "FAVOR"
        elseif itype == DealItemTypes.CITIES then typeName = "CITY"
        elseif itype == DealItemTypes.GREATWORK then typeName = "GREAT_WORK"
        end
        print("ITEM|" .. fromTag .. "|" .. typeName .. "|" .. amount .. "|" .. duration .. "|" .. vid .. "|" .. sid2)
    end
else
    print("AI_COUNTER")
    print("REJECTED")
end
pcall(function()
    local sid = DiplomacyManager.FindOpenSessionID(me, target)
    if sid and sid >= 0 then DiplomacyManager.CloseSession(sid) end
end)
-- EQUALIZE leaves both working deals populated and HasPendingDeal true. Left
-- behind, those items stack under the next propose_trade and get traded for
-- real — this preview must not outlive itself.
pcall(function() DealManager.ClearWorkingDeal(DealDirection.OUTGOING, me, target) end)
pcall(function() DealManager.ClearWorkingDeal(DealDirection.INCOMING, me, target) end)
print("{SENTINEL}")
"""


def parse_test_trade_response(lines: list[str]) -> TestTradeResult:
    """Parse the test trade response."""
    result = TestTradeResult(
        other_player_id=0,
        other_civ_name="",
        proposed=[],
        counter=[],
        rejected=False,
    )
    section = ""
    for line in lines:
        if line.startswith("CIV|"):
            parts = line.split("|")
            if len(parts) >= 3:
                result.other_player_id = int(parts[1])
                result.other_civ_name = parts[2]
        elif line == "PROPOSED_ITEMS":
            section = "proposed"
        elif line == "AI_COUNTER":
            section = "counter"
        elif line == "REJECTED":
            result.rejected = True
        elif line.startswith("ITEM|") and section:
            parts = line.split("|")
            if len(parts) >= 7:
                item = TestTradeItem(
                    side=parts[1],
                    item_type=parts[2],
                    amount=int(parts[3]),
                    duration=int(parts[4]),
                    value_id=parts[5],
                    subtype_id=parts[6],
                )
                if section == "proposed":
                    result.proposed.append(item)
                else:
                    result.counter.append(item)
    return result


def _eligibility_guard_lua(other_player_id: int, kind: str) -> str:
    """Lua guard that bails with ``ERR:...`` if a peace/alliance proposal is invalid.

    Assumes ``me``, ``target`` and ``pDiplo`` are already declared in scope.
    Single source of truth for the conditions also embedded in
    :func:`build_propose_peace` and :func:`build_form_alliance`, so the
    mailbox routing check (``build_check_proposal_eligibility``) and the
    engine-path builders can never drift apart.
    """
    if kind == "MAKE_PEACE":
        return (
            f'if not pDiplo:IsAtWarWith(target) then {_bail("ERR:NOT_AT_WAR|Not at war with player " + str(other_player_id))} end '
            "local canPeace = pDiplo:CanMakePeaceWith(target) "
            f'if not canPeace then {_bail("ERR:CANNOT_MAKE_PEACE|10-turn war cooldown or other restriction")} end'
        )
    # ALLIANCE
    return (
        f'if not Players[target] or not Players[target]:IsAlive() then {_bail("ERR:INVALID_PLAYER|Player not found")} end '
        f'if not pDiplo:HasMet(target) then {_bail("ERR:NOT_MET|Have not met this civilization")} end '
        f'if pDiplo:IsAtWarWith(target) then {_bail("ERR:AT_WAR|Cannot ally while at war")} end '
        "local ai = Players[target]:GetDiplomaticAI() "
        "local stateIdx = ai:GetDiplomaticStateIndex(me) "
        f'if stateIdx == 0 then {_bail("ERR:ALREADY_ALLIED|Already in an alliance")} end '
        f'if stateIdx ~= 1 then {_bail("ERR:NOT_FRIENDS|Must be declared friends first")} end '
        "local hasDiploService = false "
        'pcall(function() local civic = GameInfo.Civics["CIVIC_DIPLOMATIC_SERVICE"] if civic then hasDiploService = Players[me]:GetCulture():HasCivic(civic.Index) end end) '
        f'if not hasDiploService then {_bail("ERR:NO_CIVIC|Diplomatic Service civic required for alliances")} end'
    )


def build_check_proposal_eligibility(other_player_id: int, kind: str) -> str:
    """Pure eligibility check for a peace/alliance proposal (InGame context).

    Prints ``OK|<kind>`` if the proposal is allowed, otherwise bails with
    ``ERR:REASON|...``.  Does NOT open a session or touch any working deal —
    used to gate mailbox routing of ``propose_peace``/``form_alliance`` to
    managed civs before converting them into ``propose_trade`` filings.

    ``kind`` is ``"MAKE_PEACE"`` or ``"ALLIANCE"``.
    """
    kind = kind.upper()
    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local pDiplo = Players[me]:GetDiplomacy()
{_eligibility_guard_lua(other_player_id, kind)}
print("OK|{kind}")
print("{SENTINEL}")
"""


def build_form_alliance(other_player_id: int, alliance_type: str) -> str:
    """Form an alliance with another civilization (InGame context).

    alliance_type: MILITARY, RESEARCH, CULTURAL, ECONOMIC, RELIGIOUS
    """
    alliance_key = f"ALLIANCE_{alliance_type.upper()}"
    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local allianceRow = GameInfo.Alliances["{alliance_key}"]
local type_idx = allianceRow and allianceRow.Index or 0
local pDiplo = Players[me]:GetDiplomacy()
{_eligibility_guard_lua(other_player_id, "ALLIANCE")}
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
-- Always clear — see the note in build_propose_trade. A leftover working deal
-- would attach its items to the alliance proposal.
DealManager.ClearWorkingDeal(DealDirection.OUTGOING, me, target)
local deal = DealManager.GetWorkingDeal(DealDirection.OUTGOING, me, target)
if not deal then {_bail("ERR:NO_DEAL_OBJECT|Failed to get working deal")} end
do local ai_item = deal:AddItemOfType(DealItemTypes.AGREEMENTS, me)
if ai_item then ai_item:SetSubType(DealAgreementTypes.ALLIANCE) pcall(function() ai_item:SetValueType(type_idx) end) end end
DiplomacyManager.RequestSession(me, target, "MAKE_DEAL")
DealManager.SendWorkingDeal(DealProposalAction.PROPOSED, me, target)
local sid = DiplomacyManager.FindOpenSessionID(me, target)
local result = "PROPOSED"
if sid and sid >= 0 then
    local ok, respDeal = pcall(function()
        return DealManager.GetWorkingDeal(DealDirection.INCOMING, me, target)
    end)
    if ok and respDeal then
        local itemCount = 0
        pcall(function() itemCount = respDeal:GetItemCount() or 0 end)
        if itemCount > 0 then
            DealManager.SendWorkingDeal(DealProposalAction.ACCEPTED, me, target)
            result = "ACCEPTED"
        else
            result = "REJECTED"
        end
    else
        result = "REJECTED"
    end
    {_lua_close_diplo_session()}
end
local postState = Players[target]:GetDiplomaticAI():GetDiplomaticStateIndex(me)
if postState == 0 then
    local aNames = {{"RESEARCH","CULTURAL","ECONOMIC","MILITARY","RELIGIOUS"}}
    local typeName = "{alliance_type}"
    local ok3, aType = pcall(function() return pDiplo:GetAllianceType(target) end)
    if ok3 and aType and aType >= 0 then typeName = aNames[aType+1] or typeName end
    print("OK:ACCEPTED|" .. typeName .. " alliance formed with " .. name)
else
    if result == "REJECTED" then
        print("OK:REJECTED|" .. name .. " rejected the " .. "{alliance_type}" .. " alliance proposal")
    else
        print("OK:FAILED|Alliance proposal sent but status unclear (state=" .. tostring(postState) .. ")")
    end
end
print("{SENTINEL}")
"""


def build_propose_peace(other_player_id: int) -> str:
    """Propose white peace to a civilization we're at war with (InGame context).

    Session type is "MAKE_PEACE" (not "PROPOSE_PEACE_DEAL" which silently fails).
    After sending the deal, close the session with NEGATIVE+CloseSession loop,
    then ShowIngameUI + Events.HideLeaderScreen() to restore HUD and dismiss 3D leader.
    """
    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local pDiplo = Players[me]:GetDiplomacy()
{_eligibility_guard_lua(other_player_id, "MAKE_PEACE")}
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
DiplomacyManager.RequestSession(me, target, "MAKE_PEACE")
local sid = DiplomacyManager.FindOpenSessionID(me, target)
if not sid or sid < 0 then {_bail("ERR:NO_SESSION|Failed to open peace deal session")} end
DealManager.ClearWorkingDeal(DealDirection.OUTGOING, me, target)
DealManager.SendWorkingDeal(DealProposalAction.PROPOSED, me, target)
{_lua_close_diplo_session()}
print("OK:PROPOSED|" .. name)
print("{SENTINEL}")
"""


def build_check_war_state(other_player_id: int) -> str:
    """Check if we're still at war with a player (InGame context)."""
    return f"""
local me = Game.GetLocalPlayer()
local atWar = Players[me]:GetDiplomacy():IsAtWarWith({other_player_id})
print(atWar and "AT_WAR" or "AT_PEACE")
print("{SENTINEL}")
"""


def parse_diplomacy_response(lines: list[str]) -> list[CivInfo]:
    civs: dict[int, CivInfo] = {}
    for line in lines:
        if line.startswith("CIV|"):
            parts = line.split("|")
            if len(parts) < 13:
                continue
            pid = int(parts[1])
            total_score = 0  # will sum modifiers below
            civs[pid] = CivInfo(
                player_id=pid,
                civ_name=parts[2],
                leader_name=parts[3],
                has_met=parts[4] == "1",
                is_at_war=parts[5] == "1",
                diplomatic_state=parts[6],
                grievances=int(parts[7]),
                access_level=int(parts[8]),
                has_delegation=parts[9] == "1",
                has_embassy=parts[10] == "1",
                they_have_delegation=parts[11] == "1",
                they_have_embassy=parts[12] == "1",
                modifiers=[],
                available_actions=[],
            )
        elif line.startswith("MOD|"):
            parts = line.split("|")
            if len(parts) >= 4:
                pid = int(parts[1])
                if pid in civs:
                    civs[pid].modifiers.append(
                        DiplomacyModifier(
                            score=int(parts[2]),
                            text=parts[3],
                        )
                    )
                    civs[pid].relationship_score += int(parts[2])
        elif line.startswith("ALLIANCE|"):
            parts = line.split("|")
            if len(parts) >= 3:
                pid = int(parts[1])
                if pid in civs:
                    civs[pid].alliance_type = parts[2]
                    if len(parts) >= 4:
                        try:
                            civs[pid].alliance_level = int(parts[3])
                        except ValueError:
                            pass
        elif line.startswith("MILITARY|"):
            parts = line.split("|")
            if len(parts) >= 4:
                pid = int(parts[1])
                if pid in civs:
                    try:
                        civs[pid].military_strength = int(parts[2])
                        civs[pid]._our_military = int(parts[3])  # type: ignore[attr-defined]
                    except ValueError:
                        pass
        elif line.startswith("ECITY|"):
            parts = line.split("|")
            if len(parts) >= 9:
                pid = int(parts[1])
                if pid in civs:
                    xy = parts[3].split(",")
                    try:
                        vc = VisibleCity(
                            name=parts[2],
                            x=int(xy[0]),
                            y=int(xy[1]),
                            population=int(parts[4]),
                            loyalty=float(parts[5]),
                            loyalty_per_turn=float(parts[6]),
                            has_walls=int(parts[7]) > 0,
                            defense_strength=int(parts[8]),
                        )
                        civs[pid].visible_cities.append(vc)
                    except (ValueError, IndexError):
                        pass
        elif line.startswith("CIVCITIES|"):
            parts = line.split("|")
            if len(parts) >= 3:
                pid = int(parts[1])
                if pid in civs:
                    civs[pid].num_cities = int(parts[2])
        elif line.startswith("ACTIONS|"):
            parts = line.split("|")
            if len(parts) >= 3:
                pid = int(parts[1])
                if pid in civs:
                    civs[pid].available_actions = parts[2].split(",")
        elif line.startswith("AGENDA|"):
            parts = line.split("|")
            if len(parts) >= 5:
                pid = int(parts[1])
                if pid in civs:
                    civs[pid].agendas.append(
                        AgendaInfo(
                            category=parts[2],
                            name=parts[3],
                            description=parts[4],
                        )
                    )
        elif line.startswith("TRAIT|"):
            parts = line.split("|")
            if len(parts) >= 5:
                pid = int(parts[1])
                if pid in civs:
                    civs[pid].traits.append(
                        TraitInfo(
                            kind=parts[2],
                            name=parts[3],
                            description=parts[4],
                        )
                    )
        elif line.startswith("UNIQUE|"):
            parts = line.split("|")
            if len(parts) >= 4:
                pid = int(parts[1])
                if pid in civs:
                    civs[pid].uniques.append(
                        UniqueInfo(
                            category=parts[2],
                            name=parts[3],
                            description=parts[4] if len(parts) > 4 else "",
                        )
                    )
        elif line.startswith("PACT|"):
            parts = line.split("|")
            if len(parts) == 3:
                # PACT|pid|DEFENSIVE — pact between us and pid
                pid = int(parts[1])
                if pid in civs:
                    # Mark that this civ has a defensive pact (with us)
                    pass  # We don't track pacts with us specially
            elif len(parts) == 4:
                # PACT|pid1|pid2|DEFENSIVE — third-party pact
                pid1, pid2 = int(parts[1]), int(parts[2])
                if pid1 in civs:
                    civs[pid1].defensive_pacts.append(pid2)
                if pid2 in civs:
                    civs[pid2].defensive_pacts.append(pid1)
    return list(civs.values())


def parse_own_abilities_response(lines: list[str]) -> OwnAbilities:
    """Parse the local player's abilities from build_own_abilities_query."""
    own = OwnAbilities()
    for line in lines:
        if line.startswith("CIV|"):
            parts = line.split("|")
            if len(parts) >= 3:
                own.civ_name = parts[1]
                own.leader_name = parts[2]
        elif line.startswith("TRAIT|"):
            parts = line.split("|")
            if len(parts) >= 4:
                own.traits.append(
                    TraitInfo(
                        kind=parts[1],
                        name=parts[2],
                        description=parts[3],
                    )
                )
        elif line.startswith("UNIQUE|"):
            parts = line.split("|")
            if len(parts) >= 3:
                own.uniques.append(
                    UniqueInfo(
                        category=parts[1],
                        name=parts[2],
                        description=parts[3] if len(parts) > 3 else "",
                    )
                )
    return own


def parse_diplomacy_sessions(lines: list[str]) -> list[DiplomacySession]:
    """Parse open diplomacy session output (``SESSION|pid|civ|leader``)."""
    sessions = []
    for line in lines:
        if line == "NONE":
            break
        if line.startswith("SESSION|"):
            parts = line.split("|")
            if len(parts) >= 4:
                sessions.append(
                    DiplomacySession(
                        other_player_id=int(parts[1]),
                        other_civ_name=parts[2],
                        other_leader_name=parts[3],
                    )
                )
    return sessions


def parse_pending_deals_response(lines: list[str]) -> list[PendingDeal]:
    """Parse DEAL| and ITEM| lines from build_pending_deals_query."""
    deals: dict[int, PendingDeal] = {}
    for line in lines:
        if line.startswith("DEAL|"):
            parts = line.split("|")
            if len(parts) >= 4:
                pid = int(parts[1])
                deals[pid] = PendingDeal(
                    other_player_id=pid,
                    other_player_name=parts[2],
                    other_leader_name=parts[3],
                )
        elif line.startswith("ITEM|"):
            parts = line.split("|")
            if len(parts) >= 7:
                pid = int(parts[1])
                if pid not in deals:
                    continue
                is_from_us = parts[2] == "US"
                item = DealItem(
                    from_player_id=-1 if is_from_us else pid,
                    from_player_name="Us"
                    if is_from_us
                    else deals[pid].other_player_name,
                    item_type=parts[3],
                    name=parts[4],
                    amount=int(parts[5]),
                    duration=int(parts[6]),
                    is_from_us=is_from_us,
                )
                if is_from_us:
                    deals[pid].items_from_us.append(item)
                else:
                    deals[pid].items_from_them.append(item)
    return list(deals.values())
