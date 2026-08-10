"""Server-side message mailbox — free-text chat between managed players.

Sister of :mod:`civ_mcp.deal_mailbox`.  Holds chat messages routed between the
human and managed civs.  The human sends/receives via the native in-game chat
panel (force-shown in single-player, transport rerouted here); agents send via
the ``send_message`` action and read via the ``=== MESSAGES ===`` section of
``get_full_game_state``.

No mod is required — the chat shim wraps ``Network.SendChat`` in the ChatPanel
state, and inbound display calls the global ``OnChat`` in the same state.  See
``docs/managed-player-messaging.md`` for the full design.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Message:
    """One chat message."""

    message_id: str = ""
    from_player: int = -1
    to_player: int = -1
    text: str = ""
    turn: int = 0
    direction: str = ""  # "out" (sent by this seat) or "in" (received)


class MessageMailbox:
    """Ordered chat log keyed by ``message_id``.

    Lives on the shared :class:`AppContext` alongside :class:`DealMailbox` —
    messages span seats (human <-> agent, agent <-> agent).
    """

    def __init__(self, history_cap: int = 200) -> None:
        self._messages: list[Message] = []
        self._by_id: dict[str, Message] = {}
        self._history_cap = history_cap
        # Last managed civ that messaged each human player, for reply routing.
        # The native chat pulldown cannot target managed civs (they are AI to
        # the engine), so the human's reply routes to whoever last messaged
        # them unless they address a leader by name.
        self._last_inbound_sender: dict[int, int] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def post(self, msg: Message) -> str:
        """Append a message. Returns the message id."""
        if not msg.message_id:
            msg.message_id = uuid.uuid4().hex[:12]
        self._messages.append(msg)
        self._by_id[msg.message_id] = msg
        # Bound memory: drop oldest beyond the cap.
        while len(self._messages) > self._history_cap:
            old = self._messages.pop(0)
            self._by_id.pop(old.message_id, None)
        if msg.direction == "in":
            self._last_inbound_sender[msg.to_player] = msg.from_player
        log.info(
            "MsgMailbox: P%d -> P%d (%s): %r",
            msg.from_player,
            msg.to_player,
            msg.message_id,
            msg.text[:80],
        )
        return msg.message_id

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def for_player(self, player_id: int, limit: int = 50) -> list[Message]:
        """Messages involving *player_id* (incoming or outgoing), newest last,
        capped at *limit*."""
        msgs = [
            m
            for m in self._messages
            if m.from_player == player_id or m.to_player == player_id
        ]
        return msgs[-limit:]

    def last_inbound_sender(self, human_pid: int) -> int | None:
        """The most recent managed civ that messaged *human_pid*."""
        return self._last_inbound_sender.get(human_pid)

    def set_last_inbound_sender(self, human_pid: int, agent_pid: int) -> None:
        self._last_inbound_sender[human_pid] = agent_pid

    def all_messages(self) -> list[Message]:
        return list(self._messages)

    @property
    def message_count(self) -> int:
        return len(self._messages)
