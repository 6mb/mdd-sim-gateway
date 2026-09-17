"""The browser softphone reaches its line's engine through the control surface, by path."""
import asyncio
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve

from control.app import main, softphone_ws


class _EngineStub:
    """A stand-in for Asterisk's WS listener: echoes each message back with a prefix."""

    def __init__(self):
        self.subprotocols = []
        self.closed = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    async def _handler(self, connection):
        self.subprotocols.append(connection.subprotocol)
        async for message in connection:
            await connection.send(f"engine:{message}")
        self.closed.set()

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def main_():
            self.stop = asyncio.Event()
            async with serve(self._handler, "127.0.0.1", 0, subprotocols=["sip"]) as server:
                self.port = server.sockets[0].getsockname()[1]
                self.ready.set()
                await self.stop.wait()

        self.loop.run_until_complete(main_())

    def __enter__(self):
        self.thread.start()
        self.ready.wait(5)
        return self

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(5)


class SoftphoneRelayTests(unittest.TestCase):
    def client(self, *, session=True, instance=None, runtime=None, port=None):
        instance = {"id": "sim1", "sip": {"webrtc": {"enable": True}}} if instance is None else instance
        runtime = runtime or {"running": True, "ip": "127.0.0.1", "container_id": "c1"}
        stack = [
            patch.object(main.auth, "session", return_value={"csrf": "x"} if session else None),
            patch.object(main.cfg, "get_instance", return_value=instance),
            patch.object(main.engine, "container_runtime", return_value=runtime),
        ]
        if port is not None:
            stack.append(patch.object(softphone_ws, "ENGINE_WS_PORT", port))
        for p in stack:
            p.start()
            self.addCleanup(p.stop)
        return TestClient(main.app)

    def test_sip_messages_flow_both_ways_on_the_sip_subprotocol(self):
        with _EngineStub() as engine:
            client = self.client(port=engine.port)
            with client.websocket_connect(softphone_ws.path("sim1"), subprotocols=["sip"]) as ws:
                self.assertEqual(ws.accepted_subprotocol, "sip")
                ws.send_text("REGISTER sip:ims SIP/2.0")
                self.assertEqual(ws.receive_text(), "engine:REGISTER sip:ims SIP/2.0")
                # Closing the browser side must close the engine side too, or every page
                # reload would leave a registered socket behind in Asterisk.
                ws.close(1000)
                self.assertTrue(engine.closed.wait(5))
            self.assertEqual(engine.subprotocols, ["sip"])

    def test_signed_out_browser_is_refused(self):
        client = self.client(session=False)
        with self.assertRaises(WebSocketDisconnect) as closed:
            with client.websocket_connect(softphone_ws.path("sim1"), subprotocols=["sip"]):
                pass
        self.assertEqual(closed.exception.code, 4401)

    def test_unknown_line_disabled_softphone_or_missing_subprotocol_is_refused(self):
        cases = [
            ({}, ["sip"]),
            ({"id": "sim1", "sip": {"webrtc": {"enable": False}}}, ["sip"]),
            (None, []),
        ]
        for instance, subprotocols in cases:
            with self.subTest(instance=instance, subprotocols=subprotocols):
                client = self.client(instance=instance)
                with self.assertRaises(WebSocketDisconnect) as closed:
                    with client.websocket_connect(softphone_ws.path("sim1"),
                                                  subprotocols=subprotocols):
                        pass
                self.assertEqual(closed.exception.code, 1008)

    def test_stopped_engine_is_refused(self):
        client = self.client(runtime={"running": False, "ip": None, "container_id": None})
        with self.assertRaises(WebSocketDisconnect) as closed:
            with client.websocket_connect(softphone_ws.path("sim1"), subprotocols=["sip"]):
                pass
        self.assertEqual(closed.exception.code, 1013)

    def test_unreachable_engine_fails_the_handshake(self):
        with _EngineStub() as engine:
            port = engine.port
        client = self.client(port=port)  # stub is gone, nothing listens there now
        with self.assertRaises(WebSocketDisconnect) as closed:
            with client.websocket_connect(softphone_ws.path("sim1"), subprotocols=["sip"]):
                pass
        self.assertEqual(closed.exception.code, 1011)

    def test_path_is_per_line_and_escaped(self):
        self.assertEqual(softphone_ws.path("sim1"), "/api/instances/sim1/softphone/ws")
        self.assertEqual(softphone_ws.path("a/b"), "/api/instances/a%2Fb/softphone/ws")
        self.assertTrue(softphone_ws.offers_sip("chat, SIP"))
        self.assertFalse(softphone_ws.offers_sip(None))


class EngineListenerTests(unittest.TestCase):
    """What the relay connects to: plain WS on the bridge address, never on the tunnel."""

    @staticmethod
    def http_conf(**ctx):
        root = Path(__file__).resolve().parents[1]
        env = Environment(loader=FileSystemLoader(str(root / "engine" / "templates")),
                          trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
        return env.get_template("http.conf.j2").render(
            **{"webrtc_enable": True, "local_addr": "172.17.0.3",
               "webrtc_ws_port": softphone_ws.ENGINE_WS_PORT, **ctx})

    def test_listens_in_plain_on_the_bridge_address_at_the_relay_port(self):
        conf = self.http_conf()
        self.assertIn("bindaddr=172.17.0.3\n", conf)
        self.assertIn(f"bindport={softphone_ws.ENGINE_WS_PORT}\n", conf)
        self.assertNotIn("tls", conf)
        self.assertNotIn("0.0.0.0", conf)

    def test_stays_on_loopback_without_a_softphone_or_bridge_address(self):
        for ctx in ({"webrtc_enable": False}, {"local_addr": ""}):
            with self.subTest(**ctx):
                self.assertIn("bindaddr=127.0.0.1\n", self.http_conf(**ctx))

    def test_render_uses_the_relay_port(self):
        render = (Path(__file__).resolve().parents[1] / "engine" / "render.py").read_text()
        self.assertIn(f'"webrtc_ws_port": {softphone_ws.ENGINE_WS_PORT},', render)


if __name__ == "__main__":
    unittest.main()
