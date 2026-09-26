"""What an authenticated caller may ask for, decided in one place.

gate.py answers "who is this"; this module answers "may they". Kept free of FastAPI so the
rules can be tested as a table.

* The administrator may do anything, so a new management route needs no entry here.
* The engine may deliver its callback and nothing else.
* A client app (clients.py) may use a line's communication features -- texts, MMS, calls,
  voicemail, the softphone relay, and a read-only view of the line -- and nothing else. The list
  is an allow-list: a route added later is refused to clients until somebody decides a client
  needs it, and the pull request that adds such a route adds its row here.
* Anything else is refused.

Events on the live WebSocket follow the same idea (``may_receive``): a client hears about its
lines' conversations and calls, not about the host.
"""
from __future__ import annotations

import re

LINE_PREFIX = re.compile(r"^/api/instances/(?P<iid>[^/]+)(?P<rest>/.*)$")
ENGINE_EVENT_PATH = "/api/engine/event"


def _rules(*rows: tuple[str, str]) -> tuple[tuple[frozenset[str], re.Pattern[str]], ...]:
    return tuple((frozenset(methods.split()), re.compile(pattern)) for methods, pattern in rows)


# Suffixes under /api/instances/<line>/ a client may use, by method. "WEBSOCKET" is a handshake.
CLIENT_LINE_RULES = _rules(
    # A read-only view of the line itself.
    ("GET", r"^/(status|availability|allowance)$"),
    # Texts and MMS. messages/<peer> also covers messages/threads.
    ("GET", r"^/messages/[^/]+$"),
    ("GET", r"^/messages/[^/]+/mms/parts/[^/]+$"),
    ("POST", r"^/(sms/send|mms/send|messages/delete)$"),
    ("POST", r"^/messages/[^/]+/mms/download$"),
    # Calls, through the softphone relay and over the modem.
    ("GET", r"^/(calls|cellular-call/status)$"),
    ("POST", r"^/(call|hangup|calls/delete|cellular-call|cellular-call/hangup)$"),
    # Voicemail.
    ("GET", r"^/voicemails$"),
    ("GET", r"^/voicemails/[^/]+/audio$"),
    ("POST", r"^/(voicemails/delete|voicemails/[^/]+/listened)$"),
    # The softphone: its provisioning and the signalling relay.
    ("GET", r"^/softphone$"),
    ("WEBSOCKET", r"^/softphone/ws$"),
)

# Paths outside /api/instances/<line>/ a client may use. The line list is answered with a
# client's cut of each line (see main.api_instances).
CLIENT_GLOBAL_RULES = _rules(
    ("GET", r"^/api/instances$"),
    ("GET", r"^/api/auth/status$"),
    ("POST", r"^/api/auth/client/logout$"),
    ("WEBSOCKET", r"^/ws$"),
)

# Live events a client receives, and only for a line: its conversations, calls and the line's
# own state. Host alerts, hardware, cards and engine maintenance stay with the administrator.
CLIENT_EVENTS = frozenset({"sms", "call", "voicemail", "status", "line"})


def allowed(principal, method: str, path: str) -> bool:
    """Whether this caller may make this request. Fails closed for anything unlisted."""
    kind = getattr(principal, "kind", "")
    method = str(method or "").upper()
    path = path or ""
    if kind == "admin":
        return True
    if kind == "engine":
        return method == "POST" and path == ENGINE_EVENT_PATH
    if kind == "client":
        match = LINE_PREFIX.match(path)
        if match:
            rest = match.group("rest")
            return any(method in methods and pattern.match(rest)
                       for methods, pattern in CLIENT_LINE_RULES)
        return any(method in methods and pattern.match(path)
                   for methods, pattern in CLIENT_GLOBAL_RULES)
    return False


def may_receive(principal, message: dict) -> bool:
    """Whether a broadcast event goes to this caller's live WebSocket. Fails closed."""
    kind = getattr(principal, "kind", "")
    if kind == "admin":
        return True
    if kind == "client":
        return message.get("type") in CLIENT_EVENTS and bool(message.get("instance"))
    return False
