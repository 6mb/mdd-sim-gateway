"""Give the SIM time to answer an AKA challenge, and retry when it still does not.

The IMS-AKA response for a VoLTE REGISTER is computed by the SIM through the ami_usim bridge.
sysmocom waits SIM_TIMEOUT = 3 seconds for it.  A SIM in a Quectel EC25 answers through the
modem's serial bridge, and that path takes ~2.6 seconds every time (measured 2.57-3.33 s; a
SIM in a plain PC/SC reader answers in 0.2-0.4 s), so it sits ~0.4 s under the limit and
misses it now and then: five times in six days on the T-Mobile US line.

Missing it was far worse than the delay.  sim_timeout_cb() only logged "Sim did not respond,
authentication failed." and dropped the pending response: no retry was scheduled and the
registration status was left as it was.  The late AuthResponse then found nothing pending and
was discarded.  The line kept showing "Registered" while the carrier's binding ran out, and
nothing registered again until the carrier closed the TCP connection (~70 minutes each time,
observed 2026-09-16, 09-17 and 09-20; `pjsip show registrations` read "exp. 1846s ago").

Two changes:

* SIM_TIMEOUT 3 -> 10 seconds, so the ordinary EC25 answer arrives in time.
* When the SIM still does not answer, handle it exactly as the bridge's own "SIM failed" reply
  is handled (ami_authresponse sets VOLTE_STATE_SIM_FAILED and queues the response to
  handle_registration_response).  That path already ends in volte_failed, which marks the
  registration as temporarily rejected and schedules the configured fatal_retry_interval.
  The pending pointer is cleared first, so an answer that arrives after the timeout is still
  refused as "No pending AuthRequest" and the response cannot be processed twice.
"""

import os
import sys
from pathlib import Path


SOURCE = Path(os.environ.get("AST_SRC", "/home/asterisk-build/asterisk")) \
    / "res/res_pjsip_outbound_registration.c"

MARKER = "PATCH volte_sim_timeout_retry"

TIMEOUT_ANCHOR = "#define SIM_TIMEOUT 3\n"
TIMEOUT_REPLACEMENT = (
    "/* " + MARKER + ": a SIM behind a Quectel EC25 serial bridge answers in ~2.6 s,\n"
    " * which left almost no margin under the original 3 s. */\n"
    "#define SIM_TIMEOUT 10\n"
)

CALLBACK_ORIGINAL = (
    "static void sim_timeout_cb(pj_timer_heap_t *timer_heap, struct pj_timer_entry *entry)\n"
    "{\n"
    "\tstruct registration_response *response = entry->user_data;\n"
    "\n"
    "\tast_log(LOG_ERROR, \"Sim did not respond, authentication failed.\\n\");\n"
    "\n"
    "\tif (response->client_state->destroy) {\n"
    "\t\t/* We have a pending deferred destruction to complete now. */\n"
    "\t\tao2_ref(response->client_state, +1);\n"
    "\t\thandle_client_state_destruction(response->client_state);\n"
    "\t}\n"
    "\n"
    "\tao2_ref(response, -1);\n"
    "}\n"
)

CALLBACK_PATCHED = (
    "static int handle_registration_response(void *data);\n"
    "\n"
    "static void sim_timeout_cb(pj_timer_heap_t *timer_heap, struct pj_timer_entry *entry)\n"
    "{\n"
    "\tstruct registration_response *response = entry->user_data;\n"
    "\n"
    "\tast_log(LOG_ERROR, \"Sim did not respond, authentication failed.\\n\");\n"
    "\n"
    "\tif (response->client_state->destroy) {\n"
    "\t\t/* We have a pending deferred destruction to complete now. */\n"
    "\t\tao2_ref(response->client_state, +1);\n"
    "\t\thandle_client_state_destruction(response->client_state);\n"
    "\t\tao2_ref(response, -1);\n"
    "\t\treturn;\n"
    "\t}\n"
    "\n"
    "\t/* " + MARKER + ": the original dropped the response here, leaving the\n"
    "\t * registration stuck -- no retry, status unchanged -- until the carrier's binding\n"
    "\t * expired and it closed the transport (~70 min).  Take the path the bridge's own\n"
    "\t * \"SIM failed\" reply takes: volte_failed then schedules fatal_retry_interval.\n"
    "\t * Clearing the pending pointer first makes a late AuthResponse report \"No pending\n"
    "\t * AuthRequest\" instead of processing this response a second time. */\n"
    "\tif (response->client_state->volte_response == response) {\n"
    "\t\tresponse->client_state->volte_response = NULL;\n"
    "\t\tvolte_set_state(response->client_state, VOLTE_STATE_SIM_FAILED);\n"
    "\t\tif (!ast_sip_push_task(response->client_state->serializer,\n"
    "\t\t\t\thandle_registration_response, response)) {\n"
    "\t\t\t/* The response reference now belongs to the queued task. */\n"
    "\t\t\treturn;\n"
    "\t\t}\n"
    "\t\tast_log(LOG_WARNING, \"Failed to queue the SIM timeout as a registration failure; \"\n"
    "\t\t\t\"the registration will not retry until its transport closes.\\n\");\n"
    "\t}\n"
    "\n"
    "\tao2_ref(response, -1);\n"
    "}\n"
)


def _replace_once(source: str, old: str, new: str, what: str) -> str:
    at = source.find(old)
    if at < 0:
        raise ValueError(f"{what} not found")
    if source.find(old, at + 1) >= 0:
        raise ValueError(f"{what} is not unique")
    return source[:at] + new + source[at + len(old):]


def patch(source: str) -> str:
    if MARKER in source:
        return source
    source = _replace_once(source, TIMEOUT_ANCHOR, TIMEOUT_REPLACEMENT, "SIM_TIMEOUT definition")
    source = _replace_once(source, CALLBACK_ORIGINAL, CALLBACK_PATCHED, "sim_timeout_cb")
    return source


try:
    original = SOURCE.read_text()
    updated = patch(original)
except (OSError, ValueError) as exc:
    print(f"VoLTE SIM-timeout patch failed: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc

if updated == original:
    print("VoLTE SIM timeout already patched")
else:
    SOURCE.write_text(updated)
    print("patched SIM_TIMEOUT to 10 s and sim_timeout_cb to retry the registration")
