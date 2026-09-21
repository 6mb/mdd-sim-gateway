"""A slow SIM answer must not leave the line silently unregistered for an hour.

A SIM behind a Quectel EC25 answers the IMS-AKA challenge in ~2.6 s; sysmocom waited 3 s and,
on timeout, only logged and dropped the pending response. No retry was scheduled and the status
stayed "Registered" until the carrier closed the transport, ~70 minutes later.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATCHER = ROOT / "engine" / "patches" / "asterisk" / "volte_sim_timeout_retry.py"

# The pinned sysmocom source, verbatim, around the two anchors.
SOURCE = (
    "#define SIM_TIMEOUT 3\n"
    "\n"
    "/*! \\brief Timer callback function, used just for registrations */\n"
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


class VolteSimTimeoutPatchTests(unittest.TestCase):
    def _apply(self, source):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "res" / "res_pjsip_outbound_registration.c"
            target.parent.mkdir(parents=True)
            target.write_text(source)
            first = subprocess.run([sys.executable, str(PATCHER)],
                                   env={**os.environ, "AST_SRC": temp},
                                   capture_output=True, text=True)
            patched = target.read_text()
            second = subprocess.run([sys.executable, str(PATCHER)],
                                    env={**os.environ, "AST_SRC": temp},
                                    capture_output=True, text=True)
            return first, patched, target.read_text(), second

    def test_the_timeout_leaves_room_for_an_ec25_answer(self):
        first, patched, twice, second = self._apply(SOURCE)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("#define SIM_TIMEOUT 10\n", patched)
        self.assertNotIn("#define SIM_TIMEOUT 3\n", patched)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(twice, patched)

    def test_a_timeout_takes_the_sim_failed_path_so_the_registration_retries(self):
        _first, patched, _twice, _second = self._apply(SOURCE)
        callback = patched[patched.index("static void sim_timeout_cb"):]

        # The same handling as the bridge's own "SIM failed" reply, which reaches volte_failed
        # and schedules fatal_retry_interval.
        self.assertIn("volte_set_state(response->client_state, VOLTE_STATE_SIM_FAILED);",
                      callback)
        self.assertIn("ast_sip_push_task(response->client_state->serializer,\n"
                      "\t\t\t\thandle_registration_response, response)", callback)
        # The task is declared before use: handle_registration_response is defined later.
        self.assertLess(patched.index("static int handle_registration_response(void *data);"),
                        patched.index("static void sim_timeout_cb"))

    def test_a_late_answer_cannot_process_the_response_twice(self):
        _first, patched, _twice, _second = self._apply(SOURCE)
        callback = patched[patched.index("static void sim_timeout_cb"):]

        clear = callback.index("response->client_state->volte_response = NULL;")
        queue = callback.index("ast_sip_push_task(")
        self.assertLess(clear, queue)
        # Only the response still pending is requeued.
        self.assertIn("if (response->client_state->volte_response == response)", callback)

    def test_each_path_releases_the_response_exactly_once(self):
        _first, patched, _twice, _second = self._apply(SOURCE)
        callback = patched[patched.index("static void sim_timeout_cb"):]
        callback = callback[:callback.index("\n}\n") + 3]

        destroy = callback[callback.index("if (response->client_state->destroy)"):]
        destroy = destroy[:destroy.index("return;")]
        self.assertIn("ao2_ref(response, -1);", destroy)
        # When the task takes the reference the callback returns without releasing it.
        queued = callback[callback.index("ast_sip_push_task("):]
        self.assertLess(queued.index("return;"), queued.index("ao2_ref(response, -1);"))

    def test_an_upstream_refactor_fails_the_build_instead_of_being_skipped(self):
        first, _patched, _twice, _second = self._apply(
            SOURCE.replace("#define SIM_TIMEOUT 3", "#define SIM_TIMEOUT_SECONDS 3"))

        self.assertEqual(first.returncode, 1)
        self.assertIn("SIM_TIMEOUT definition not found", first.stderr)

        first, _patched, _twice, _second = self._apply(
            SOURCE.replace("\tao2_ref(response, -1);\n}\n", "\tao2_cleanup(response);\n}\n"))
        self.assertEqual(first.returncode, 1)
        self.assertIn("sim_timeout_cb not found", first.stderr)


if __name__ == "__main__":
    unittest.main()
