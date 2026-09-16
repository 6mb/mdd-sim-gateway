"""Receive, retrieve and send MMS for a line.

An MMS arrives in two halves: a notification (m-notification-ind) carried by a WAP Push SMS,
and the message itself, fetched from the carrier's MMSC over HTTP on the carrier's MMS APN.
The notification can reach the gateway over VoWiFi and on the modem alike; both paths hand
the WAP Push payload to handle_wap_push(), which stores one pending MMS per MMSC location.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time

from . import cellular_sms, mms_pdu, mms_transport, store

log = logging.getLogger("vowifi.mms")

# WDP destination port of the WAP Push connectionless session service (WAP-259-WDP).
WAP_PUSH_PORT = 2948
# Retrieval retries after a transient failure. An MMSC keeps a message for days, but a
# notification can be retried sooner than its expiry is worth waiting for.
RETRY_DELAYS = (60, 300, 900, 3600, 4 * 3600)
# One MMSC exchange at a time per gateway: the modem's command channel is serial and shared
# with the SIM bridge, and interleaving two sockets' AT commands would only slow both.
io_lock = threading.Lock()

_STATUS_NAMES = {
    mms_pdu.STATUS_EXPIRED: "expired", mms_pdu.STATUS_RETRIEVED: "retrieved",
    mms_pdu.STATUS_REJECTED: "rejected", mms_pdu.STATUS_DEFERRED: "deferred",
    mms_pdu.STATUS_UNRECOGNISED: "unrecognised",
    mms_pdu.STATUS_INDETERMINATE: "indeterminate",
    mms_pdu.STATUS_FORWARDED: "forwarded", mms_pdu.STATUS_UNREACHABLE: "unreachable",
}


def is_wap_push_udh(udh_hex: str) -> bool:
    """Whether an SMS User Data Header addresses the WAP Push port."""
    try:
        dest, _src = mms_pdu.extract_wdp_port(bytes.fromhex(str(udh_hex or "")))
    except ValueError:
        return False
    return dest == WAP_PUSH_PORT


def handle_wap_push(instance: str, sender: str, data: bytes, *, transport: str,
                    sent_ts: int | None = None, now: int | None = None) -> dict:
    """Consume one WAP Push payload addressed to the MMS user agent.

    Returns {"handled": False} when the payload is not an MMS push (the caller files it as a
    binary SMS). Otherwise "message" is a newly stored pending MMS (None for a notification
    already held), or "delivery" the outgoing MMS a delivery report updated.
    """
    try:
        push = mms_pdu.parse_wap_push(bytes(data))
    except mms_pdu.MmsDecodeError:
        return {"handled": False}
    if push.content_type != "application/vnd.wap.mms-message":
        return {"handled": False}
    try:
        pdu = mms_pdu.decode_pdu(push.body, now=sent_ts or now)
    except mms_pdu.MmsDecodeError as exc:
        log.info("undecodable MMS push on line %s: %s", instance, exc)
        return {"handled": False}

    if pdu.message_type == mms_pdu.M_NOTIFICATION_IND:
        location = pdu.content_location
        if not location:
            log.info("MMS notification without a content location on line %s", instance)
            return {"handled": False}
        peer = store.canonical_peer(instance, pdu.from_address or sender)
        rec = store.ingest_mms_notification(
            instance, peer=peer, transport=transport, content_location=location,
            transaction_id=pdu.transaction_id, subject=pdu.subject, size=pdu.message_size,
            expiry_ts=pdu.expiry, sent_ts=sent_ts, to_addrs=pdu.to)
        if rec:
            log.info("MMS notification on line %s from %s (%s bytes)", instance, peer,
                     pdu.message_size)
        return {"handled": True, "message": rec}

    if pdu.message_type == mms_pdu.M_DELIVERY_IND:
        status = _STATUS_NAMES.get(pdu.status, "indeterminate")
        recipient = pdu.to[0] if pdu.to else ""
        rec = store.record_mms_delivery(instance, pdu.message_id, recipient, status, pdu.date)
        return {"handled": True, "delivery": rec}

    # A read report or anything else the MMS user agent receives: nothing to show.
    return {"handled": True}


def _modem_for(inst: dict, runner=subprocess.run) -> str | None:
    iccid = cellular_sms._normalize_iccid(inst.get("iccid"))
    if not iccid:
        return None
    path, _problem = cellular_sms._find_modem(
        iccid, runner, 10.0, imsi=cellular_sms._normalize_imsi(inst.get("imsi")))
    return path


def _request_headers(settings: dict, content_type: str | None = None) -> dict:
    headers = {"Accept": f"{mms_transport.MMS_CONTENT_TYPE}, */*",
               "User-Agent": settings.get("user_agent") or mms_transport.DEFAULT_USER_AGENT}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def open_client(inst: dict, settings: dict, *, runner=subprocess.run):
    if not settings.get("enabled"):
        raise mms_transport.MmsTransportError("MMS is turned off for this line",
                                              retryable=False)
    if not settings.get("configured"):
        raise mms_transport.MmsTransportError(
            "no MMSC is known for this line's carrier; set it in the line's MMS settings",
            retryable=False)
    return mms_transport.client_for(settings, _modem_for(inst, runner), runner=runner)


def _parts_for_store(pdu: mms_pdu.MmsPdu) -> list[dict]:
    parts = []
    for part in pdu.parts:
        text = part.text() if part.content_type.startswith("text/plain") else None
        parts.append({"content_type": part.content_type, "data": part.data,
                      "name": part.name or part.content_location, "content_id": part.content_id,
                      "charset": part.charset, "text": text})
    return parts


def download(inst: dict, message_id: int, *, client=None, now: int | None = None,
             runner=subprocess.run) -> dict:
    """Fetch one notified MMS from the MMSC and store its content.

    Returns {"ok": True} or {"ok": False, "error", "final"}; "final" means no retry is
    scheduled. The MMSC is told the message was retrieved, which is what stops it resending
    the notification; that acknowledgement is best effort, since the content is already safe.
    """
    now = int(now or time.time())
    row = store.mms_for_download(message_id)
    if not row or row["direction"] != "in":
        return {"ok": False, "error": "no such MMS", "final": True}
    settings = mms_transport.resolve_settings(inst)
    expired = bool(row.get("expiry_ts")) and now > int(row["expiry_ts"])
    store.set_mms_state(message_id, "downloading")
    try:
        with io_lock:
            if client is None:
                client = open_client(inst, settings, runner=runner)
            response = client.request("GET", row["content_location"],
                                      headers=_request_headers(settings))
            if response.status != 200:
                raise mms_transport.MmsTransportError(
                    f"the MMSC answered HTTP {response.status}",
                    retryable=response.status >= 500 or response.status in (408, 429))
            pdu = mms_pdu.decode_pdu(response.body, now=now)
            if pdu.message_type != mms_pdu.M_RETRIEVE_CONF:
                raise mms_transport.MmsTransportError(
                    f"the MMSC answered with MMS message type {pdu.message_type:#x}")
            status = pdu.retrieve_status
            if status not in (None, mms_pdu.RETRIEVE_STATUS_OK):
                text = pdu.headers.get("retrieve-text") or \
                    mms_pdu.RETRIEVE_STATUS_DESCRIPTIONS.get(status, f"status {status:#x}")
                raise mms_transport.MmsTransportError(
                    f"the MMSC could not deliver the MMS: {text}",
                    retryable=0xC0 <= status < 0xE0)
            parts = _parts_for_store(pdu)
            body = "\n".join(p["text"] for p in parts if p["text"]) or pdu.subject
            store.save_mms_content(
                message_id, parts, subject=pdu.subject, body=body,
                from_addr=pdu.from_address or None, to_addrs=pdu.to,
                cc_addrs=pdu.headers.get("cc") or [], size=len(response.body))
            store.set_mms_state(message_id, "retrieved", error="", next_attempt_ts=None,
                                attempts_increment=1, message_status="ok")
            if row.get("transaction_id") and settings.get("mmsc"):
                try:
                    client.request("POST", settings["mmsc"],
                                   body=mms_pdu.encode_notifyresp_ind(
                                       row["transaction_id"], mms_pdu.STATUS_RETRIEVED),
                                   headers=_request_headers(settings,
                                                            mms_transport.MMS_CONTENT_TYPE))
                except Exception as exc:  # noqa: BLE001 -- the message itself is stored
                    log.info("MMS %s retrieved but the MMSC acknowledgement failed: %s",
                             message_id, exc)
        return {"ok": True}
    except (mms_transport.MmsTransportError, mms_pdu.MmsDecodeError) as exc:
        attempts = int(row.get("attempts") or 0) + 1
        retryable = getattr(exc, "retryable", True) and not expired
        if retryable and attempts <= len(RETRY_DELAYS):
            store.set_mms_state(message_id, "failed", error=str(exc),
                                next_attempt_ts=now + RETRY_DELAYS[attempts - 1],
                                attempts_increment=1)
            return {"ok": False, "error": str(exc), "final": False}
        store.set_mms_state(message_id, "expired" if expired else "failed", error=str(exc),
                            next_attempt_ts=None, attempts_increment=1)
        return {"ok": False, "error": str(exc), "final": True}
