"""Import SMS objects owned by ModemManager into the unified message store.

VoWiFi SMS arrives through Asterisk. When a modem is also registered on the cellular network,
ModemManager independently receives ordinary 3GPP SMS and keeps them as D-Bus SMS objects.
This module reads those objects without taking over the modem's serial port and maps them to the
saved SIM line by ICCID.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from datetime import datetime

SMS_PATH_RE = re.compile(r"^/org/freedesktop/ModemManager1/SMS/\d+$")
MODEM_PATH_RE = re.compile(r"/org/freedesktop/ModemManager1/Modem/\d+")


def _run_json(args: list[str], runner=subprocess.run) -> dict:
    result = runner(["mmcli", *args, "--output-json"], capture_output=True, text=True,
                    timeout=10, check=False)
    if result.returncode:
        return {}
    try:
        value = json.loads(result.stdout or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _modem_paths(runner=subprocess.run) -> list[str]:
    result = runner(["mmcli", "-L"], capture_output=True, text=True, timeout=10, check=False)
    return sorted(set(MODEM_PATH_RE.findall(result.stdout or ""))) if not result.returncode else []


def _timestamp(value) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except (ValueError, OverflowError):
        return 0


class Scanner:
    """Incrementally inspect ModemManager without rereading stable objects every five seconds.

    The SMS path listing remains live on every poll, so a newly arrived message is discovered
    without extra delay. Modem/SIM identity and already-seen SMS details are much more stable and
    are refreshed periodically to tolerate ModemManager restarts and object-path reuse.
    """

    def __init__(self, runner=subprocess.run, *, topology_ttl: float = 60.0,
                 detail_ttl: float = 60.0, clock=time.monotonic):
        self.runner = runner
        self.topology_ttl = topology_ttl
        self.detail_ttl = detail_ttl
        self.clock = clock
        self._topology_expires = 0.0
        self._topology: list[tuple[str, str]] = []
        self._details: dict[tuple[str, str], tuple[float, dict]] = {}

    def _refresh_topology(self, now: float) -> None:
        topology = []
        for modem_path in _modem_paths(self.runner):
            modem = _run_json(["-m", modem_path], self.runner).get("modem") or {}
            sim_path = str((modem.get("generic") or {}).get("sim") or "")
            if not sim_path:
                continue
            sim_doc = _run_json(["-i", sim_path], self.runner).get("sim") or {}
            iccid = str((sim_doc.get("properties") or {}).get("iccid") or "")
            if iccid:
                topology.append((modem_path, iccid))
        if topology != self._topology:
            self._details.clear()
        self._topology = topology
        # Empty topology is retried quickly so modem hot-plug discovery stays responsive.
        self._topology_expires = now + (self.topology_ttl if topology else min(5.0, self.topology_ttl))

    def discover(self, instances: list[dict]) -> list[dict]:
        """Return displayable cellular SMS records. No message body or identity is logged."""
        now = self.clock()
        if now >= self._topology_expires:
            self._refresh_topology(now)
        by_iccid = {str(item.get("iccid") or ""): str(item.get("id")) for item in instances
                    if item.get("iccid") and item.get("id") is not None}
        found = []
        live_keys = set()
        for modem_path, iccid in self._topology:
            iid = by_iccid.get(iccid)
            if not iid:
                continue
            listing = _run_json(["-m", modem_path, "--messaging-list-sms"], self.runner)
            paths = listing.get("modem.messaging.sms") or []
            for sms_path in paths:
                sms_path = str(sms_path)
                if not SMS_PATH_RE.match(sms_path):
                    continue
                key = (modem_path, sms_path)
                live_keys.add(key)
                cached = self._details.get(key)
                if cached and now < cached[0]:
                    record = cached[1]
                else:
                    sms = _run_json(["-s", sms_path], self.runner).get("sms") or {}
                    content, props = sms.get("content") or {}, sms.get("properties") or {}
                    text, peer = str(content.get("text") or ""), str(content.get("number") or "")
                    if not text.strip():
                        self._details.pop(key, None)
                        continue
                    pdu_type = str(props.get("pdu-type") or "").lower()
                    direction = "out" if pdu_type == "submit" else "in"
                    timestamp = str(props.get("timestamp") or "")
                    signature = "\0".join((iccid, sms_path, direction, peer, text, timestamp))
                    record = {
                        "fingerprint": hashlib.sha256(signature.encode()).hexdigest(),
                        "direction": direction, "peer": peer, "body": text,
                        "ts": _timestamp(timestamp), "transport": "cellular",
                    }
                    self._details[key] = (now + self.detail_ttl, record)
                found.append({**record, "instance": iid})
        # Bound memory when ModemManager deletes SMS objects or a SIM is no longer configured.
        self._details = {key: value for key, value in self._details.items() if key in live_keys}
        return found


def discover(instances: list[dict], runner=subprocess.run) -> list[dict]:
    """One-shot compatibility wrapper used by diagnostics and callers outside the poller."""
    return Scanner(runner).discover(instances)
