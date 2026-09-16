"""Receive, retrieve and send MMS for a line.

An MMS arrives in two halves: a notification (m-notification-ind) carried by a WAP Push SMS,
and the message itself, fetched from the carrier's MMSC over HTTP on the carrier's MMS APN.
The notification can reach the gateway over VoWiFi and on the modem alike; both paths hand
the WAP Push payload to handle_wap_push(), which stores one pending MMS per MMSC location.
"""
from __future__ import annotations

import logging

from . import mms_pdu, store

log = logging.getLogger("vowifi.mms")

# WDP destination port of the WAP Push connectionless session service (WAP-259-WDP).
WAP_PUSH_PORT = 2948

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
