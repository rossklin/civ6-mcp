# Managed-Player Messaging (Chat) — Implementation Plan

A free-text chat between managed players (agents and the human) for the
human-vs-agent handoff mode. Agents send and read messages through MCP tools;
the human sends and reads them through Civ 6's **native in-game chat panel**,
force-shown in single-player and rerouted to a Python mailbox instead of the
(absent) network transport.

This document is self-contained: a developer can implement it using only the
codebase locations cited here. You do **not** need to read the Civ 6 UI source —
the relevant facts from it are quoted below with file paths for reference only.

---

## 1. Design decision: why option B (native chat panel)

The original request considered two options:

- **Option A** — show messages on the leader/diplomacy screen with a text-input
  field there for the reply.
- **Option B** — force-show Civ 6's native multiplayer chat panel in
  single-player and wrap it.

**Option A is not achievable without a mod.** Civ 6 UI controls (specifically
`<EditBox>`, the only text-input control) must be declared in XML that is loaded
from disk at context initialization. FireTuner-injected Lua can only manipulate
controls that already exist; it cannot create an `EditBox` at runtime. The
leader screen (`DiplomacyActionView`) contains only one `EditBox` —
`ValueAmountEditBox` in `DiplomacyDealView.xml` (line ~397), which is
`NumberInput="1"` with `MaxLength="11"`, i.e. numeric-only and useless for free
text. Adding a free-text input to the leader screen would require shipping a
`.modinfo` + XML file installed on disk — an architectural departure this
project explicitly avoids (the server is pure FireTuner injection).

Per the requester's rule ("if I'd have to close the leader screen to write a
response, I prefer option B"), we implement **option B**.

**Option B is fully feasible within the existing architecture.** The chat
panel's *render* layer has zero multiplayer dependency; only its *transport*
(`Network.SendChat` / `Events.MultiplayerChat`) requires a network session. We
bypass both: we wrap `Network.SendChat` (outbound) and call the panel's display
function directly (inbound). Both target functions are **globals** in the
`ChatPanel` Lua state, reachable the same way the existing deal shim reaches the
`DiplomacyDealView` state.

---

## 2. Architecture primer (read this first)

### 2.1 How the server talks to the game

The Python MCP server (`src/civ_mcp/server.py`) communicates with Civilization VI
over a persistent TCP connection to the game's **FireTuner** debug protocol on
`127.0.0.1:4318` (game launched with `EnableTuner=1`). There are **no mod files
on disk**. Instead, Lua source code is sent as text and executed in the game's
Lua VM on demand.

- `src/civ_mcp/connection.py` — `class GameConnection` (line 45). Handshakes and
  discovers the available **Lua states**, each indexed by number. Every UI
  context (e.g. `InGame`, `DiplomacyDealView`, `ChatPanel`, `WorldTracker`) is
  its own Lua state with its own copy of the engine wrapper tables
  (`DealManager`, `Network`, `Controls`, …). Reaching a context's globals means
  addressing its state directly.
- `execute_read(lua)` (connection.py:228) — runs Lua in the `GameCore_Tuner`
  state (simulation reads).
- `execute_write(lua, perspective=False)` (connection.py:242) — runs Lua in the
  `InGame` state (UI commands). Pass `perspective=False` for code that must see
  the *actual* local player (turn-ownership probes, notification sends, UI
  unhiding) rather than the calling seat's remapped view.
- `execute_in_named_state(name, lua)` (connection.py:282) — runs Lua in a named
  UI context's state (e.g. `"DiplomacyDealView"`, `"ChatPanel"`,
  `"WorldTracker"`). Never applies the seat perspective rewrite. Returns `[]`
  if the state is absent. `state_index_for(name)` (connection.py:263) resolves a
  name to an index (re-handshakes once on miss).

### 2.2 The print/drain channel (game → server async signals)

Lua `print()` output is sent back over TCP and collected by the server. Each
`execute_*` call collects lines up to a sentinel (`SENTINEL = "---END---"`,
`src/civ_mcp/lua/_helpers.py:5`).

The game also emits `print()` lines the server did **not** request (e.g. from
shim wrappers reacting to player input). These are drained by a background loop
and routed to registered callbacks:

- `_deal_monitor_loop` (connection.py:181) polls `poll_deal_messages()`
  (connection.py:160) every 0.5s.
- Each drained line is parsed by `_parse_mcpdeal_line(payload)`
  (connection.py:413) into an event dict (or `None`).
- Parsed events are handed to every callback registered via
  `conn.add_deal_callback(cb)` (connection.py:122), where
  `cb(event_type: str, data: dict)`.
- Stale lines are also drained inside `_locked_execute` (connection.py:330) so
  nothing is lost between poll cycles.

This is the channel the chat feature uses for the **human → server** direction:
the chat shim `print()`s an `MCPCHAT|...` line when the human types, the monitor
drains it, `_parse_mcpdeal_line` parses it, and a callback files it in the
mailbox.

### 2.3 The mailbox pattern

Two mailboxes already exist on `AppContext` (server.py:59):

- `DealMailbox` (`src/civ_mcp/deal_mailbox.py`) — `PendingProposal` objects keyed
  by `proposal_id` (UUID hex). Lives on the shared `AppContext`, **not** per
  seat, because proposals span seats. Methods: `propose`, `get`, `accept`,
  `reject`, `get_pending_for(pid)`, `get_sent_by(pid)`, `all_pending`.
- `DiploMailbox` (`src/civ_mcp/diplo_mailbox.py`) — sister pattern for
  response-able diplomatic actions.

The new `MessageMailbox` (section 5.1) mirrors these exactly but holds chat
messages instead of deal items.

### 2.4 The shim pattern (wrapping a global in a named state)

`src/lua/deal_shim.lua` is the template. It wraps `DealManager.SendWorkingDeal`,
`IsAutoPropose`, and `UpdateDealStatus` **in the `DiplomacyDealView` state**. The
critical idempotent-re-arm pattern (deal_shim.lua:75-78):

```lua
if __MCP_orig_SWD == nil then
    __MCP_orig_SWD = DealManager.SendWorkingDeal
end
local origSWD = __MCP_orig_SWD            -- LOCAL upvalue
DealManager.SendWorkingDeal = function(...) return origSWD(...) end
```

The `__MCP_orig_*` guard captures the original **once**; the **local upvalue**
(`origSWD`) is what the wrapper calls through, so a second install can never
make a wrapper call itself (which once caused a Lua stack-overflow hard crash —
see the docstring at handoff.py:631-644). The new `chat_shim.lua` (section 5.2)
uses the identical pattern on `Network.SendChat`.

The template is loaded and tag-substituted by `build_deal_shim_install_lua()`
(handoff.py:631) and installed by `install_deal_shim()` (handoff.py:695) via
`conn.execute_in_named_state(DEAL_SHIM_STATE, ...)` where
`DEAL_SHIM_STATE = "DiplomacyDealView"` (handoff.py:612).

### 2.5 Re-arm on save load (HandoffKeeper)

Anything installed in a UI Lua state is destroyed when a save is loaded (the
contexts are rebuilt). The `HandoffKeeper` (handoff.py:430) polls for the
handoff hook and re-arms it whenever it goes missing. Other shims register a
re-arm callable via `keeper.add_post_install_hook(hook)` (handoff.py:453), where
`hook` is an async callable run after each keeper re-arm cycle. The deal shim
and notification handler are re-armed this way (server.py:522-523). The chat
shim and chat-panel unhide must register the same way (section 5.5).

---

## 3. Civ 6 chat-panel facts (you do not need to read the source)

All paths under
`C:\Program Files (x86)\Steam\steamapps\common\Sid Meier's Civilization VI\Base\Assets\UI`.

### 3.1 The chat panel is always loaded; only its container is hidden in SP

- `WorldTracker.xml` line 40-42 declares the chat panel as a child LuaContext,
  **unconditionally**:
  ```xml
  <Container ID="ChatPanelContainer" Size="300,parent" MinSize="0, 118" Hidden="1">
      <LuaContext FileName="ChatPanel"/>
  </Container>
  ```
- `WorldTracker.lua:1109` `LateInitialize()` gates it (lines 1113-1120):
  ```lua
  if(UI.HasFeature("Chat")
      and (GameConfiguration.IsNetworkMultiplayer() or GameConfiguration.IsPlayByCloud()) ) then
      UpdateChatPanel(false);
  else
      UpdateChatPanel(true);          -- hide container
      Controls.ChatCheck:SetHide(true); -- remove toggle checkbox
  end
  ```
  In single-player both predicates are false → the `else` branch runs. The
  ChatPanel Lua context still fully initializes and subscribes to
  `Events.MultiplayerChat`, but stays inert (no network events fire in SP).

- `UpdateChatPanel(hideChat)` is a **global** in the WorldTracker state
  (`WorldTracker.lua:605`):
  ```lua
  function UpdateChatPanel(hideChat:boolean)
      m_hideChat = hideChat;
      Controls.ChatPanelContainer:SetHide(m_hideChat);
      Controls.ChatCheck:SetCheck(not m_hideChat);
      RealizeEmptyMessage();
      RealizeStack();
      CheckUnreadChatMessageCount();
  end
  ```
  **To unhide the chat panel in single-player, run in the WorldTracker state:**
  ```lua
  UpdateChatPanel(false)
  Controls.ChatCheck:SetHide(false)
  ```
  This reverses the gate exactly (it sets `m_hideChat`, the container, and the
  checkbox), so the WorldTracker's own resize/show-hide logic stays consistent.
  Do **not** try to `SetHide(false)` the container directly — that leaves
  `m_hideChat == true` and the tracker will re-hide it.

### 3.2 The display path is global and MP-free

`ChatPanel.lua` defines these as **globals** (no `local` keyword — confirmed by
grep), so they are callable from Lua run in the `ChatPanel` state:

- `OnChat(fromPlayer, toPlayer, text, eTargetType, playSounds)` —
  `ChatPanel.lua:111`. Renders one message into the chat log with the sender's
  name, color-codes by `eTargetType`, plays a sound, and fires
  `LuaEvents.ChatPanel_OnChatReceived(fromPlayer, isHidden)`. It uses
  `PlayerConfigurations[fromPlayer]:GetPlayerName()` for the sender label, so
  passing an agent's player ID shows the agent's leader/civ name. It calls
  `AddChatEntry(...)` (also a global, `ChatLogic.lua:149`) to append the line.
- `OnMultiplayerChat(fromPlayer, toPlayer, text, eTargetType)` —
  `ChatPanel.lua:187`, just calls `OnChat(..., true)`.
- `SendChat(text)` — `ChatPanel.lua:295`. The human's Enter-key handler. Parses
  slash-commands, then calls `Network.SendChat(parsedText, targetType,
  targetID)` at line 318, then clears the box.

`eTargetType` is a `ChatTargetTypes` enum value (a global in the ChatPanel
state): `CHATTARGET_ALL`, `CHATTARGET_TEAM`, `CHATTARGET_PLAYER`. Use
`ChatTargetTypes.CHATTARGET_PLAYER` for a directed message so it renders with the
whisper color and includes the recipient name.

### 3.3 The transport layer is what we replace

`Network.SendChat(text, targetType, targetID)` is the engine network call. In
pure single-player there is no network session, so calling it does nothing
useful (no `Events.MultiplayerChat` echoes back). `Network` is a per-state
engine table — wrapping `Network.SendChat` **in the ChatPanel state** affects
only the ChatPanel's calls (exactly as the deal shim wraps
`DealManager.SendWorkingDeal` in the `DiplomacyDealView` state). The only in-game
caller is `ChatPanel.lua:318`. (Staging-room and end-game chat use separate
states and are unaffected — we don't care about them.)

### 3.4 Recipient picker excludes AI / managed civs

`PlayerTargetLogic.lua` (the pulldown population) only lists players where
`pPlayerCfg:IsHuman()` is true. Managed civs are AI to the engine during another
player's turn (see memory: "other managed civs are AI to the engine"), so the
human **cannot** select a managed civ from the native whisper pulldown, and `/w
<name>` name-matching also filters to humans. This is why we use **recipient
selection option (a)**: route by "reply to last sender" by default, with
optional name-in-text resolution, decided Python-side in our `Network.SendChat`
wrapper. The pulldown's actual `targetID` is effectively ignored for routing
(though we still read it).

### 3.5 Immersion cue: diplomacy ribbon portrait flash

`DiplomacyRibbon.lua:946` consumes `LuaEvents.ChatPanel_OnChatReceived` and
flashes a chat indicator on the speaking leader's portrait. Because our inbound
path goes through `OnChat` (which fires this event), the human gets a leader-
portrait flash when an agent messages them — a partial substitute for the
leader-screen immersion of option A, at no extra cost.

---

## 4. Data flow (end to end)

### 4.1 Agent → Human

1. Agent calls `execute_commands` with `action="send_message"`,
   `params={"other_player_id": <human_pid>, "text": "..."}`.
2. Dispatcher intercepts `send_message` (section 5.5), calls `_send_message`.
3. `_send_message` files a `Message(from=agent, to=human, direction="out")` in
   `MessageMailbox`, then runs
   `build_send_chat_message_lua(agent_pid, human_pid, text)` in the `ChatPanel`
   state → `OnChat(agent_pid, human_pid, text, CHATTARGET_PLAYER, true)` renders
   it into the human's chat log and flashes the agent's portrait.
4. The chat panel is ensured visible (section 5.3 unhide, idempotent).
5. Records `last_inbound_sender_for[human_pid] = agent_pid` (for the human's
   reply routing — section 5.6).

### 4.2 Human → Agent

1. Human types in the chat box and presses Enter → `SendChat` → our wrapped
   `Network.SendChat(text, targetType, targetID)` in the ChatPanel state.
2. The wrapper hex-encodes the text and `print("MCPCHAT|SEND|from=..|to=..|ttype=..|hex=..")`.
3. `_deal_monitor_loop` drains it; `_parse_mcpdeal_line` (extended, section 5.4)
   parses it into `{"type":"chat_send","from":..,"to":..,"ttype":..,"text":..}`.
4. `_on_chat_event` callback (section 5.5) resolves the recipient (section 5.6)
   and files a `Message(from=human, to=<resolved agent>, direction="in")` in
   `MessageMailbox`.
5. On the agent's next turn, `get_full_game_state` surfaces it in the
   `=== MESSAGES ===` section (section 5.5).

### 4.3 Agent → Agent (both managed)

1. Agent A calls `send_message` with `other_player_id = <agent B pid>`.
2. `_send_message` files the message in the mailbox. **No Lua/UI action** — the
   target is a managed civ, so there is no human chat panel to push to.
3. Agent B's `get_full_game_state` surfaces it in `=== MESSAGES ===` next turn.

---

## 5. Implementation

### 5.1 `src/civ_mcp/message_mailbox.py` (new file)

Mirror `deal_mailbox.py`. Hold an ordered list of `Message` objects keyed by
`message_id`. Lives on `AppContext`.

```python
"""Server-side message mailbox — free-text chat between managed players.

Sister of :mod:`civ_mcp.deal_mailbox`. Holds chat messages routed between the
human and managed civs. The human sends/receives via the native in-game chat
panel (force-shown in single-player, transport rerouted here); agents send via
the ``send_message`` action and read via the ``=== MESSAGES ===`` section of
``get_full_game_state``.

No mod is required — the chat shim wraps ``Network.SendChat`` in the ChatPanel
state, and inbound display calls the global ``OnChat`` in the same state.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class Message:
    """One chat message."""

    message_id: str = ""
    from_player: int = -1
    to_player: int = -1
    text: str = ""
    turn: int = 0
    direction: str = ""  # "out" (from this seat's perspective when sent) or "in"


class MessageMailbox:
    """Ordered chat log keyed by ``message_id``.

    Lives on the shared :class:`AppContext` alongside :class:`DealMailbox` —
    messages span seats (human ↔ agent, agent ↔ agent).
    """

    def __init__(self, history_cap: int = 200) -> None:
        self._messages: list[Message] = []
        self._by_id: dict[str, Message] = {}
        self._history_cap = history_cap
        # Last managed civ that messaged each human player, for reply routing.
        self._last_inbound_sender: dict[int, int] = {}

    def post(self, msg: Message) -> str:
        if not msg.message_id:
            msg.message_id = uuid.uuid4().hex[:12]
        self._messages.append(msg)
        self._by_id[msg.message_id] = msg
        # Bound memory: drop oldest beyond the cap.
        if len(self._messages) > self._history_cap:
            old = self._messages.pop(0)
            self._by_id.pop(old.message_id, None)
        if msg.direction == "in":
            self._last_inbound_sender[msg.to_player] = msg.from_player
        log.info("MsgMailbox: P%d -> P%d (%s): %r",
                 msg.from_player, msg.to_player, msg.message_id, msg.text[:80])
        return msg.message_id

    def for_player(self, player_id: int, limit: int = 50) -> list[Message]:
        """Messages involving *player_id* (incoming or outgoing), newest last,
        capped at *limit*."""
        msgs = [m for m in self._messages
                if m.from_player == player_id or m.to_player == player_id]
        return msgs[-limit:]

    def last_inbound_sender(self, human_pid: int) -> int | None:
        return self._last_inbound_sender.get(human_pid)

    def set_last_inbound_sender(self, human_pid: int, agent_pid: int) -> None:
        self._last_inbound_sender[human_pid] = agent_pid

    def all_messages(self) -> list[Message]:
        return list(self._messages)
```

### 5.2 `src/lua/chat_shim.lua` (new file)

Mirror `deal_shim.lua`'s idempotent pattern. Wraps `Network.SendChat` in the
ChatPanel state. No managed-id tag is needed (routing is decided Python-side).

```lua
-- Chat shim: wraps Network.SendChat in the ChatPanel state so the human's
-- typed messages are routed to the Python message mailbox instead of the
-- (absent in single-player) network transport.
--
-- This file is a TEMPLATE loaded by build_chat_shim_install_lua() in
-- handoff.py. One tag is substituted before the Lua is sent to the game:
--   __MCP_SENTINEL_TAG__  -> the response sentinel (see _helpers.SENTINEL)
--
-- Idempotent re-arm: the original SendChat is captured once (when
-- __MCP_orig_SendChat is nil) and called through a LOCAL upvalue, so repeated
-- installs never stack wrappers and can never self-recurse. Same pattern as
-- deal_shim.lua.

__MCP_chat_install_count = (__MCP_chat_install_count or 0) + 1

if __MCP_orig_SendChat == nil then
    __MCP_orig_SendChat = Network.SendChat
end
local origSendChat = __MCP_orig_SendChat

Network.SendChat = function(text, targetType, targetID)
    -- Hex-encode the text so pipes, newlines and quotes cannot break the
    -- pipe-delimited drain line. Python decodes with bytes.fromhex().
    local hex = ""
    for i = 1, #text do
        hex = hex .. string.format("%02x", string.byte(text, i))
    end
    print("MCPCHAT|SEND"
        .. "|from=" .. tostring(Game.GetLocalPlayer())
        .. "|to="   .. tostring(targetID)
        .. "|ttype=" .. tostring(targetType)
        .. "|hex="  .. hex)
    -- Do NOT call origSendChat: there is no network session in single-player,
    -- and the message must route to the mailbox, not the network.
end

print("CHATSHIM|installed|install_count=" .. tostring(__MCP_chat_install_count))
print("__MCP_SENTINEL_TAG__")
```

Notes:
- `Network` is a per-state table, so this wrap only affects the ChatPanel state.
- We intentionally never call `origSendChat` in single-player. If hotseat/
  network multi-agent is ever supported, gate on
  `GameConfiguration.IsNetworkMultiplayer()` and fall through to
  `origSendChat(text, targetType, targetID)` in that branch.
- Hex encoding is used (not base64) because Lua has no built-in base64 but
  `string.byte`/`string.format` are always available. Hex chars are `[0-9a-f]`,
  so they never collide with the `|` delimiters.

### 5.3 `src/civ_mcp/handoff.py` additions

Add three builders + one installer, mirroring the deal-shim block at
handoff.py:612-710.

**Constants and template loader** (place near `DEAL_SHIM_STATE` at line 612):

```python
CHAT_SHIM_STATE = "ChatPanel"
WORLDTRACKER_STATE = "WorldTracker"

_CHAT_SHIM_PATH = Path(__file__).resolve().parent.parent / "lua" / "chat_shim.lua"
_CHAT_SHIM_TEMPLATE: str | None = None


def _load_chat_shim_template() -> str:
    global _CHAT_SHIM_TEMPLATE
    if _CHAT_SHIM_TEMPLATE is None:
        _CHAT_SHIM_TEMPLATE = _CHAT_SHIM_PATH.read_text(encoding="utf-8")
    return _CHAT_SHIM_TEMPLATE
```

**`build_chat_shim_install_lua()`** — mirror `build_deal_shim_install_lua()`
(handoff.py:631), but only the sentinel tag is substituted:

```python
def build_chat_shim_install_lua() -> str:
    """Lua that wraps Network.SendChat in the ChatPanel state.

    Idempotent re-arm — see deal_shim pattern (handoff.py:631). The wrapper
    hex-encodes the human's typed text and prints an MCPCHAT|SEND|... line
    drained by the deal monitor, instead of calling the (absent in SP) network.
    """
    lua = _load_chat_shim_template()
    lua = lua.replace("__MCP_SENTINEL_TAG__", SENTINEL)
    return lua


def build_chat_shim_uninstall_lua() -> str:
    """Restore the original Network.SendChat in the ChatPanel state."""
    return (
        "if __MCP_orig_SendChat ~= nil then "
        "  Network.SendChat = __MCP_orig_SendChat "
        "  __MCP_orig_SendChat = nil "
        "end "
        'print("CHATSHIM|removed") '
        f'print("{SENTINEL}")'
    )


async def install_chat_shim(conn: GameConnection) -> str:
    """Install the chat shim in the ChatPanel state. Returns status string."""
    index = await conn.state_index_for(CHAT_SHIM_STATE)
    if index is None:
        log.warning("Chat shim: %s state not found", CHAT_SHIM_STATE)
        return "absent"
    lines = await conn.execute_in_named_state(
        CHAT_SHIM_STATE, build_chat_shim_install_lua()
    )
    status = next(
        (l[9:] for l in lines if l.startswith("CHATSHIM|")), "unknown"
    )
    log.info("Chat shim: %s", status)
    return status
```

**`build_unhide_chat_lua()`** — run in the WorldTracker state to force-show the
chat panel in single-player (section 3.1):

```python
def build_unhide_chat_lua() -> str:
    """Lua (WorldTracker state) that force-shows the chat panel in SP.

    Reverses the WorldTracker.LateInitialize gate (WorldTracker.lua:1117-1119)
    by calling the global UpdateChatPanel(false) and unhiding the toggle
    checkbox. Idempotent: safe to call repeatedly.
    """
    return (
        "UpdateChatPanel(false) "
        "Controls.ChatCheck:SetHide(false) "
        'print("CHATUNHID") '
        f'print("{SENTINEL}")'
    )
```

**`build_send_chat_message_lua()`** — run in the ChatPanel state to render an
agent's message into the human's chat log (section 3.2). Add a Python-side
Lua-string quoter (place in `_helpers.py` or alongside the builder):

```python
# In src/civ_mcp/lua/_helpers.py:
def lua_quote(s: str) -> str:
    """Produce a safe Lua double-quoted string literal."""
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "")
    s = s.replace("\n", "\\n")
    return '"' + s + '"'
```

```python
# In handoff.py:
from civ_mcp.lua._helpers import SENTINEL, lua_quote

def build_send_chat_message_lua(
    from_pid: int, to_pid: int, text: str
) -> str:
    """Lua (ChatPanel state) that renders one message into the human's chat log.

    Calls the global OnChat so the message uses the native render path: sender
    name from PlayerConfigurations[from_pid], whisper color, portrait flash via
    LuaEvents.ChatPanel_OnChatReceived.
    """
    safe_text = lua_quote(text)
    return (
        f"local fromP = {from_pid} "
        f"local toP = {to_pid} "
        f"local text = {safe_text} "
        "OnChat(fromP, toP, text, ChatTargetTypes.CHATTARGET_PLAYER, true) "
        'print("CHATSENT") '
        f'print("{SENTINEL}")'
    )
```

### 5.4 `src/civ_mcp/connection.py` — extend the line parser

Add a branch to `_parse_mcpdeal_line()` (connection.py:413) that recognizes
`MCPCHAT|SEND|...` lines. Insert this block alongside the existing
`MCPDEAL_CLICK|` / `MCPDEAL|` branches (e.g. just before the final
`return None` at line 538):

```python
    # Chat shim: human typed a message in the chat panel.
    if text.startswith("MCPCHAT|"):
        rest = text[len("MCPCHAT|"):]
        parts = rest.split("|")
        verb = parts[0] if parts else ""
        if verb == "SEND":
            data: dict = {"type": "chat_send"}
            for part in parts[1:]:
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k in ("from", "to", "ttype"):
                        try:
                            data[k] = int(v)
                        except ValueError:
                            pass
                    elif k == "hex":
                        data["hex"] = v
            if "hex" in data:
                try:
                    data["text"] = bytes.fromhex(data["hex"]).decode(
                        "utf-8", errors="replace"
                    )
                except ValueError:
                    data["text"] = ""
            return data
        return None
```

The hex field is always last and contains only `[0-9a-f]`, so `split("|")` is
safe. Empty messages yield `hex=` (empty) → `text == ""`.

No changes to the monitor loop or callback dispatch are needed —
`_deal_monitor_loop` (connection.py:181) already routes every parsed event to
every registered callback. `_on_deal_event` (server.py:473) has no branch for
`"chat_send"` and silently ignores it; we add a dedicated callback in 5.5.

### 5.5 `src/civ_mcp/server.py` — wiring

#### 5.5.1 AppContext field (server.py:59)

Add alongside `mailbox` / `diplo_mailbox` (lines 73-78):

```python
    # Message mailbox — free-text chat between managed players and the human.
    message_mailbox: MessageMailbox | None = None
```

#### 5.5.2 Construct, install, and re-arm (server.py, in the `if cfg.enabled:`
block starting at line 449)

After the diplo mailbox is constructed (line 447) and the deal shim is installed
(line 452), add the chat equivalents. Mirror the deal-shim install (452),
notification-handler install (458-468), and re-arm hooks (505-523).

```python
    # Message mailbox — free-text chat.
    message_mailbox = MessageMailbox()

    if cfg.enabled:
        # ... existing deal shim + notification handler installs ...

        # Install the chat shim (ChatPanel state) and unhide the chat panel
        # (WorldTracker state). Both are idempotent.
        try:
            chat_status = await handoff.install_chat_shim(conn)
            log.info("Chat shim: %s", chat_status)
        except Exception:
            log.warning("Chat shim install failed", exc_info=True)
        try:
            await conn.execute_in_named_state(
                handoff.WORLDTRACKER_STATE,
                handoff.build_unhide_chat_lua(),
            )
        except Exception:
            log.warning("Chat panel unhide failed", exc_info=True)
```

Register a chat-event callback alongside `_on_deal_event` (server.py:473-490).
Add after `conn.add_deal_callback(_on_deal_event)` (line 490):

```python
        def _on_chat_event(event_type: str, data: dict) -> None:
            if event_type == "chat_send":
                # Human typed a message in the native chat panel.
                asyncio.ensure_future(_handle_human_chat(app, data, cfg))

        conn.add_deal_callback(_on_chat_event)
```

Add re-arm hooks alongside the existing ones (server.py:522-523):

```python
        async def _rearm_chat_shim():
            await handoff.install_chat_shim(conn)

        async def _rearm_chat_unhide():
            try:
                await conn.execute_in_named_state(
                    handoff.WORLDTRACKER_STATE,
                    handoff.build_unhide_chat_lua(),
                )
            except Exception:
                log.debug("Chat unhide re-arm failed", exc_info=True)

        keeper.add_post_install_hook(_rearm_chat_shim)
        keeper.add_post_install_hook(_rearm_chat_unhide)
```

Wire `app.message_mailbox = message_mailbox` wherever `app.mailbox` /
`app.diplo_mailbox` are assigned (search for the existing assignment — it is in
the AppContext construction/population path; mirror it exactly).

#### 5.5.3 `send_message` action (dispatcher, server.py:1471+)

Add `"send_message"` to `_ALLOWED_ACTIONS` (server.py:1423). Place it in the
"Diplomacy & trade" group (line 1440) or a new "Messaging" group.

In the per-command dispatcher loop (server.py:1471), add a branch **before**
the `propose_trade` branch (line 1474) so it is intercepted and never falls
through to the engine executor:

```python
            if action == "send_message" and app.message_mailbox is not None:
                result = await _send_message(app, seat, params)
                results.append(f"send_message: {result}")
                continue
```

Implement `_send_message` (place near `_mailbox_propose_trade` at line 2650):

```python
async def _send_message(app, seat, params: dict) -> str:
    """Post a chat message from the calling seat to a target player.

    - Target is a managed civ: file in the mailbox only (the target agent
      reads it in get_full_game_state next turn). No UI action.
    - Target is the human: file in the mailbox AND render into the human's
      native chat panel via OnChat in the ChatPanel state.
    """
    mb = app.message_mailbox
    if mb is None:
        return "Error: message mailbox not available"
    target = params.get("other_player_id", -1)
    text = params.get("text", "")
    if target < 0 or not text:
        return "Error: other_player_id and non-empty text are required"
    agent_pid = seat.player_id
    cfg = app.handoff_config

    # File in the mailbox (both directions).
    mb.post(Message(
        from_player=agent_pid,
        to_player=target,
        text=text,
        turn=await _current_turn(app),   # helper used elsewhere; reuse it
        direction="out",
    ))

    if target == cfg.human_id:
        # Push to the human's native chat panel.
        try:
            await app.game.conn.execute_in_named_state(
                handoff.CHAT_SHIM_STATE,
                handoff.build_send_chat_message_lua(agent_pid, target, text),
            )
        except Exception:
            log.warning("Chat message push to human failed", exc_info=True)
            return f"Posted to mailbox but UI push failed: {text[:60]!r}"
        # Record last sender so the human's reply routes back to this agent.
        mb.set_last_inbound_sender(target, agent_pid)
        return f"Message sent to human P{target}: {text[:60]!r}"

    if target in cfg.managed_ids:
        return f"Message sent to P{target} (managed): {text[:60]!r}"

    # Target is an unmanaged (built-in AI) civ — still file it for the log,
    # but there is no agent to read it. Warn.
    return (f"Message logged to P{target} (unmanaged AI — no agent reads it): "
            f"{text[:60]!r}")
```

> Note: obtain the connection and current turn the same way other handlers do.
> `app.game` exposes a `GameState`; follow the pattern used in
> `_handle_deal_notification_click` (server.py:872) which receives `gs:
> GameState` and uses `conn` passed in. If `_send_message` cannot reach `conn`
> directly from `app`, thread it through as `_send_message(app, seat, params,
> conn)` — match how `_mailbox_propose_trade` accesses state. Reuse the
> existing current-turn getter (search `get_current_turn` / `Game.GetGameTurn`
> usage) rather than introducing a new one.

#### 5.5.4 `_handle_human_chat` callback handler

Filed by `_on_chat_event` (5.5.2). Resolves the recipient (section 5.6) and
posts the message.

```python
async def _handle_human_chat(app, data: dict, cfg: HandoffConfig) -> None:
    """Human typed a message in the native chat panel — route to mailbox."""
    mb = app.message_mailbox
    if mb is None:
        return
    human_pid = data.get("from", cfg.human_id)
    text = data.get("text", "")
    if not text:
        return
    target = await _resolve_chat_recipient(app, data, cfg)
    if target is None:
        # No resolvable recipient — echo a hint back to the human's chat log.
        try:
            await app.game.conn.execute_in_named_state(
                handoff.CHAT_SHIM_STATE,
                handoff.build_send_chat_message_lua(
                    human_pid, human_pid,
                    "(No recipient resolved. Address a leader by name "
                    "or reply after they message you.)",
                ),
            )
        except Exception:
            log.debug("Chat hint echo failed", exc_info=True)
        return
    mb.post(Message(
        from_player=human_pid,
        to_player=target,
        text=text,
        turn=await _current_turn(app),
        direction="in",
    ))
```

#### 5.5.5 `=== MESSAGES ===` section in `get_full_game_state`

Mirror the `=== DEAL MAILBOX ===` block (server.py:1182-1208). Place it after
the diplo-mailbox block (after line 1244). Surface the calling seat's recent
conversation (newest last), capped:

```python
        # Append chat messages (managed-player messaging).
        try:
            app = _app(ctx)
            mb = app.message_mailbox
            if mb is not None:
                seat = _get_seat(ctx)
                msgs = mb.for_player(seat.player_id, limit=50)
                if msgs:
                    text += "\n\n=== MESSAGES ==="
                    for m in msgs:
                        who = "To" if m.from_player == seat.player_id else "From"
                        other = (m.to_player if m.from_player == seat.player_id
                                 else m.from_player)
                        text += f"\n[{who} P{other} (T{m.turn})] {m.text}"
        except Exception:
            log.debug("Failed to append messages", exc_info=True)
```

Also document `send_message` in the `execute_commands` tool docstring (the
comment block above the tool at server.py:1251), alongside the existing
mailbox actions, so the agent knows the action exists and its params
(`other_player_id`, `text`).

### 5.6 Recipient selection (option a) — `_resolve_chat_recipient`

Implemented Python-side (server.py, near the other chat helpers). The native
pulldown cannot target managed civs (section 3.4), so we resolve as follows,
in priority order:

1. **Explicit `targetID` that is a managed civ.** If
   `data["to"]` is in `cfg.managed_ids`, use it. (Rare via native UI, but
   covers future `/w`-style enhancements.)
2. **Name-in-text.** If the message text begins with a leader or civ name
   present in the roster, route to that civ and strip the prefix from the text
   before posting. Use `handoff.get_roster()` / the roster builder
   (`build_roster_lua`, handoff.py:259) to get `{player_id: name}` and do a
   case-insensitive prefix match, longest name first (so "Trajan" doesn't match
   before a longer name if one existed).
3. **Reply to last sender.** `mb.last_inbound_sender(human_pid)` — the most
   recent managed civ that messaged this human.
4. **Fallback.** `None` → `_handle_human_chat` echoes a hint (5.5.4).

```python
async def _resolve_chat_recipient(
    app, data: dict, cfg: HandoffConfig
) -> int | None:
    mb = app.message_mailbox
    human_pid = cfg.human_id

    # 1. Explicit whisper target that is a managed civ.
    target_id = data.get("to", -1)
    if target_id in cfg.managed_ids:
        return target_id

    text = data.get("text", "")

    # 2. Name-in-text prefix. Resolve via the roster (cached if available).
    roster = await _get_roster_dict(app)   # {player_id: "Trajan", ...}
    if roster:
        # Longest-name-first so longer names win prefix matches.
        for pid, name in sorted(roster.items(),
                                key=lambda kv: -len(kv[1])):
            if pid not in cfg.managed_ids or pid == human_pid:
                continue
            if text.lower().startswith(name.lower()):
                # Strip the matched prefix from the text.
                stripped = text[len(name):].lstrip(" :,-")
                data["text"] = stripped
                return pid

    # 3. Reply to last sender.
    last = mb.last_inbound_sender(human_pid) if mb else None
    if last is not None and last in cfg.managed_ids:
        return last

    return None
```

> Implement `_get_roster_dict(app)` by running `build_roster_lua` (handoff.py:259)
> once per human turn (or caching it on `AppContext`/the keeper, which already
> tracks ownership). Match the existing roster-access pattern used by
> `_send_deal_notifications` (server.py:948-966), which looks up leader names via
> `handoff.get_roster()`.

---

## 6. Edge cases & escaping

- **Outbound text encoding.** The chat shim hex-encodes the text before
  `print()`, so pipes, newlines, quotes, and non-ASCII cannot break the
  pipe-delimited `MCPCHAT|SEND|...` line. Python decodes with
  `bytes.fromhex(...).decode("utf-8", "replace")`.
- **Inbound text encoding.** `lua_quote()` (5.3) escapes `\`, `"`, `\r`, `\n`
  for the Lua double-quoted literal. Cap display length server-side if desired
  (e.g. truncate at 1000 chars before pushing to the chat panel).
- **Empty messages.** `SendChat` already guards `string.len(text) > 0`
  (ChatPanel.lua:296). An empty `hex=` decodes to `""`; `_handle_human_chat`
  drops it.
- **Unbounded growth.** `MessageMailbox` caps total stored messages at
  `history_cap` (default 200, oldest dropped) and `for_player()` returns at most
  `limit=50`.
- **Re-arm after save load.** Both the chat shim (ChatPanel state) and the unhide
  (WorldTracker state) are lost on load. The two `add_post_install_hook`
  callables (5.5.2) re-arm them on every keeper cycle, exactly as the deal shim
  and notification handler are re-armed (server.py:522-523).
- **Multi-agent hotseat / network.** If the game is ever launched as network
  multiplayer, the native gate already shows the chat panel, and
  `Network.SendChat` would actually transmit. The shim currently suppresses the
  real call unconditionally. If hotseat/network multi-agent is supported later,
  gate the wrapper: `if GameConfiguration.IsNetworkMultiplayer() then return
  origSendChat(text, targetType, targetID) end` before the print.
- **Human not currently the local player.** `OnChat` pushes to the chat panel
  regardless of whose turn it is (the panel is part of the human's UI always).
  The unhide ensures the panel is visible. No turn check needed for display.

---

## 7. Verification / test plan

1. **Named-state reachability.** Before building on it, confirm `ChatPanel` and
   `WorldTracker` are enumerated states (same mechanism as `DiplomacyDealView`):
   ```python
   for name in ("DiplomacyDealView", "ChatPanel", "WorldTracker"):
       print(name, await conn.state_index_for(name))
   ```
   `DiplomacyDealView` is a nested UI context and is reachable, so the other two
   should be too. If either is `None`, the keeper re-handshake
   (`state_index_for` reconnects once on miss) should resolve it; if not, the
   context may have a different registered name — dump
   `conn.lua_states` to find the exact name.

2. **Unhide.** Launch a single-player game. Run `build_unhide_chat_lua()` in the
   `WorldTracker` state. Confirm the chat panel appears on the right side and
   the toggle checkbox is visible. Type a message and press Enter — with the
   shim installed, the message should **not** echo back (no network), and the
   server log should show a drained `MCPCHAT|SEND|...` line.

3. **Shim idempotency.** Install the chat shim twice; confirm
   `install_count=2` and that typing once produces exactly one `MCPCHAT|SEND`
   line (no stacked wrappers). Save and reload; confirm the keeper re-arms it
   (`install_count` resets to 1) and it still works.

4. **Inbound display.** Call `build_send_chat_message_lua(agent_pid, human_pid,
   "Hello from agent")` in the `ChatPanel` state. Confirm the line appears in
   the chat log with the agent's leader name, whisper color, and the agent's
   portrait flashes in the diplomacy ribbon.

5. **End to end — agent → human.** Via the MCP tool: `execute_commands` with
   `action="send_message", params={"other_player_id": <human>, "text":"hi"}`.
   Confirm the message appears in the human's chat panel and in the
   `=== MESSAGES ===` section of the agent's `get_full_game_state`.

6. **End to end — human → agent.** Human types "ok, agreed" in the chat panel
   (after the agent messaged them, so last-sender routing applies). Confirm the
   drained `MCPCHAT|SEND` line is parsed, a `Message` is filed, and the agent's
   next `get_full_game_state` shows `[From P<human> (T..)] ok, agreed`.

7. **Recipient resolution.** Test each branch of `_resolve_chat_recipient`:
   name-in-text prefix (verify the prefix is stripped from the stored text),
   last-sender fallback, and the no-recipient hint echo.

8. **Agent → agent.** `send_message` to another managed civ: confirm the
   message appears in the target agent's `=== MESSAGES ===` and no UI action
   fired.

---

## 8. Open risks / spikes

- **State-name verification (5.5/7.1).** `"ChatPanel"` and `"WorldTracker"`
  must be the exact registered state names. `state_index_for` handles this with
  a one-shot re-handshake; if a name differs, dump `conn.lua_states`. Low risk
  — `DiplomacyDealView` (also a nested context) is reachable today.
- **Roster caching for name resolution (5.6).** `_get_roster_dict` should reuse
  the keeper's ownership/roster state rather than running `build_roster_lua`
  per message. Inspect how `_send_deal_notifications` (server.py:948) and
  `handoff.get_roster()` already obtain names and reuse that path. If no cached
  roster exists, run the Lua once per human turn and cache it on `AppContext`.
- **`OnChat` visibility when the panel is collapsed/hidden.** `OnChat`
  manipulates `Controls.ChatEntryStack` etc., which exist even when the
  container is hidden (the context is always loaded). Building entries into a
  hidden stack should render once unhidden. If entries do not appear after the
  panel is later shown, call `build_unhide_chat_lua()` before the first
  `OnChat` push (the install flow in 5.5.2 already unhides before any push).

No other blockers were found. Every hook point is a writable global in an
addressable Lua state, matching the existing deal-notification architecture.

---

## 9. File checklist

New files:
- `src/civ_mcp/message_mailbox.py` — `Message`, `MessageMailbox` (5.1).
- `src/lua/chat_shim.lua` — `Network.SendChat` wrapper template (5.2).

Modified files:
- `src/civ_mcp/handoff.py` — `CHAT_SHIM_STATE`, `WORLDTRACKER_STATE`,
  `build_chat_shim_install_lua`, `build_chat_shim_uninstall_lua`,
  `install_chat_shim`, `build_unhide_chat_lua`, `build_send_chat_message_lua`
  (5.3).
- `src/civ_mcp/lua/_helpers.py` — `lua_quote` (5.3).
- `src/civ_mcp/connection.py` — `MCPCHAT|` branch in `_parse_mcpdeal_line`
  (5.4).
- `src/civ_mcp/server.py` — `AppContext.message_mailbox` (5.5.1); construct,
  install, re-arm, `_on_chat_event` callback (5.5.2); `send_message` in
  `_ALLOWED_ACTIONS` + dispatcher branch + `_send_message` (5.5.3);
  `_handle_human_chat` (5.5.4); `=== MESSAGES ===` section (5.5.5);
  `_resolve_chat_recipient` + `_get_roster_dict` (5.6).

Documentation:
- `AGENTS.md` — add a "Messaging" section documenting the `send_message` action
  and the `=== MESSAGES ===` game-state section (mirror how the deal mailbox is
  documented).
- This file (`docs/managed-player-messaging.md`) is the design record.

---

## Appendix A — Civ 6 source reference (for verification only)

Do not modify these files; they are the shipped game. Cited so the facts in
section 3 can be re-verified if a patch changes them.

- `Base\Assets\UI\WorldTracker.xml` line 40-42 — `ChatPanelContainer` +
  `ChatPanel` LuaContext (unconditional).
- `Base\Assets\UI\WorldTracker.lua:605` — `UpdateChatPanel` (global);
  `:1109-1120` — `LateInitialize` SP gate.
- `Base\Assets\UI\Popups\ChatPanel.lua:111` — `OnChat` (global);
  `:187` — `OnMultiplayerChat`; `:295` — `SendChat` (global); `:318` —
  `Network.SendChat` call; `:949` — `Events.MultiplayerChat.Add`.
- `Base\Assets\UI\Popups\ChatLogic.lua:149` — `AddChatEntry` (global);
  `:197` — `Events.MultiplayerChat.Add`.
- `Base\Assets\UI\Popups\PlayerTargetLogic.lua:75-100` — pulldown filters to
  `IsHuman()` (why recipient selection option a is needed).
- `Base\Assets\UI\DiplomacyRibbon.lua:946` — consumes
  `LuaEvents.ChatPanel_OnChatReceived` (portrait flash).
- `Base\Assets\UI\DiplomacyDealView.xml:397` — `ValueAmountEditBox`
  (numeric-only; the only EditBox on the leader/deal screen — confirms option A
  needs a mod).
