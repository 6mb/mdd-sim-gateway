"""An upgrade must put the new engine image into service, not merely build it.

A running container keeps the image it was started from. Rebuilding the image while leaving
the containers alone ships nothing: every line goes on serving the previous dialplan while
the control plane reports the new version. That mismatch is invisible from the UI — a user
whose engine predated service-code support saw the feature fail and was told "the carrier
does not support this code", when the request had never left the gateway.
"""
import re
import unittest
from pathlib import Path

INSTALL = (Path(__file__).resolve().parent.parent / "install.sh").read_text(encoding="utf-8")


def _body(name: str) -> str:
    """The text of a shell function, up to the next top-level closing brace."""
    start = INSTALL.index(f"{name}() {{")
    return INSTALL[start:INSTALL.index("\n}\n", start)]


class EngineImageRefreshTests(unittest.TestCase):
    def test_rebuilding_the_image_forces_the_containers_to_be_re_created(self):
        # The decision must follow what actually happened, not a flag the operator has to
        # know to pass: nobody upgrading reads release notes for "--engines".
        reload_body = _body("cmd_reload")
        self.assertIn('[ "$ENGINE_IMAGE_CHANGED" = 1 ]', reload_body)
        removal = reload_body.index("docker rm -f")
        condition = reload_body.rindex("if [", 0, removal)
        self.assertIn("ENGINE_IMAGE_CHANGED", reload_body[condition:removal])

    def test_every_rebuild_path_reports_that_it_replaced_the_image(self):
        ensure = _body("ensure_engine_image")
        # Overlay refresh, the pre-fingerprint adoption path, the offline overlay and the
        # full build all replace the image; each has to say so or the containers are kept.
        self.assertEqual(ensure.count("ENGINE_IMAGE_CHANGED=1"), 4, ensure)

    def test_reusing_an_unchanged_image_leaves_the_lines_alone(self):
        # The flag starts at 0 and the reuse path returns before any assignment, so an
        # ordinary reload does not interrupt calls for nothing.
        ensure = _body("ensure_engine_image")
        self.assertIn("ENGINE_IMAGE_CHANGED=0", ensure)
        reuse = ensure.index("matches this checkout — reusing")
        self.assertNotIn("ENGINE_IMAGE_CHANGED=1", ensure[:reuse])

    def test_an_explicit_request_to_keep_the_engines_still_wins(self):
        reload_body = _body("cmd_reload")
        self.assertIn('PRESERVE_ENGINES=1', reload_body)
        preserve = reload_body.index('[ "$PRESERVE_ENGINES" = 1 ]')
        self.assertLess(preserve, reload_body.index('[ "$ENGINE_IMAGE_CHANGED" = 1 ]'))


if __name__ == "__main__":
    unittest.main()
