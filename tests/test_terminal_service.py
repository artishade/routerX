"""The terminal can be hosted on its own, and the project stays connected to it.

Run: python -m unittest discover -s tests -p test_terminal_service.py

`terminal/link.py` is the seam: with NOVA_TERMINAL_URL unset the terminal
runs in-process (LocalLink), with it set the gateway forwards every terminal
route to a separate service (RemoteLink). Both are driven here through the
*same* `terminal/api.py` routes, because that router is what both hosts
mount — so a response shape proven in one mode is the response shape in the
other.

The interesting failure modes, and the ones most likely to come back:
  · a remote error losing its meaning across the wire (the `code` must survive,
    or the dashboard just says "something went wrong")
  · the agent's shell step quietly falling back to a subprocess when the
    terminal is remote, i.e. the user stops seeing their task run
  · the SSE stream ending instead of running the length of the session
"""
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from terminal import link as terminal_link   # the seam (LocalLink / RemoteLink)
from terminal import pty as pty_session     # the real shells behind it
from terminal.api import router as terminal_router
from terminal.link import (
    LinkUnavailable,
    LocalLink,
    RemoteLink,
    SessionGone,
    SessionLimitReached,
    error_from_payload,
)


# A real shell tab needs a POSIX pseudo-terminal; `tests/test_portability.py`
# pins the typed 503 a host without one returns.
_POSIX_ONLY = unittest.skipUnless(pty_session.PTY_SUPPORTED, "interactive PTY sessions need a POSIX host")


def _app() -> FastAPI:
    """A bare app carrying only the shared terminal contract."""
    app = FastAPI()
    app.include_router(terminal_router, prefix="/terminal/pty")
    return app


class LinkErrorTests(unittest.TestCase):
    """Errors must survive the hop, or a remote failure is unreadable."""

    def test_a_remote_code_becomes_the_same_exception(self):
        for kind in (SessionGone, SessionLimitReached, LinkUnavailable):
            with self.subTest(kind=kind.__name__):
                err = error_from_payload({"error": "boom", "code": kind.code}, 500)
                self.assertIsInstance(err, kind)
                self.assertEqual(str(err), "boom")

    def test_an_unknown_code_stays_a_terminal_error(self):
        err = error_from_payload({"error": "weird", "code": "nope"}, 418)
        self.assertEqual(str(err), "weird")
        self.assertEqual(err.status, 500)

    def test_a_non_json_body_does_not_crash_the_client(self):
        err = error_from_payload(None, 502)
        self.assertIn("502", str(err))


@_POSIX_ONLY
class LocalLinkTests(unittest.IsolatedAsyncioTestCase):
    """In-process mode — the default, and the one the dashboard used before."""

    def setUp(self):
        pty_session.manager.close_all()
        self.addCleanup(pty_session.manager.close_all)
        self.link = LocalLink()

    async def test_a_session_round_trips_through_the_link(self):
        info = await self.link.create(label="local-tab", cols=100, rows=30)
        self.assertTrue(info["id"])
        self.assertEqual(info["label"], "local-tab")
        self.assertEqual((info["cols"], info["rows"]), (100, 30))

        found = await self.link.get(info["id"])
        self.assertEqual(found["id"], info["id"])

        renamed = await self.link.rename(info["id"], "renamed")
        self.assertEqual(renamed["label"], "renamed")

        cols, rows = await self.link.resize(info["id"], 90, 24)
        self.assertEqual((cols, rows), (90, 24))

        self.assertTrue(await self.link.close(info["id"]))
        self.assertIsNone(await self.link.get(info["id"]))

    async def test_a_dead_session_reports_gone_not_a_crash(self):
        with self.assertRaises(SessionGone):
            await self.link.write("pty-does-not-exist", "ls\n")
        with self.assertRaises(SessionGone):
            await self.link.resize("pty-does-not-exist", 80, 24)
        self.assertIsNone(await self.link.get("pty-does-not-exist"))
        self.assertFalse(await self.link.close("pty-does-not-exist"))

    async def test_the_agent_step_lands_in_a_visible_tab(self):
        out, code = await self.link.run_command("echo link-marker", "agent · link", 30)
        self.assertEqual(code, 0)
        self.assertIn("link-marker", out)
        labels = [s["label"] for s in pty_session.manager.list_sessions()]
        self.assertIn("agent · link", labels)

    async def test_the_stream_carries_output(self):
        info = await self.link.create(label="stream-tab")
        await self.link.write(info["id"], "echo streamed-marker\n")

        # The first frame is always the backlog, so a late attacher sees the
        # session's history rather than starting from nothing.
        body = await self._drain(info["id"], "streamed-marker")
        self.assertIn("streamed-marker", body)
        self.assertTrue(body.startswith("data: "))
        self.assertIn('"session": "%s"' % info["id"], body)

    async def test_the_stream_announces_exit(self):
        info = await self.link.create(label="exit-tab")
        await self.link.write(info["id"], "exit\n")
        # The `done` event only arrives once the shell is gone, so this is
        # where the exit code has to survive.
        body = await self._drain(info["id"], '"done": true')
        self.assertIn('"exit": 0', body)
        self.assertIn('"done": true', body)

    async def _drain(self, session: str, stop_on: str, limit: int = 200) -> str:
        """Read the stream until `stop_on` shows up (bounded, so a regression
        fails the test instead of hanging the suite)."""
        body = ""
        async for frame in self.link.stream(session, 0, lambda: _false()):
            body += frame
            if stop_on in body or body.count("data: ") > limit:
                break
        await self.link.close(session)
        return body


async def _false() -> bool:
    return False


class RemoteLinkTests(unittest.IsolatedAsyncioTestCase):
    """Remote mode — the same surface, forwarded to another host."""

    def setUp(self):
        self.seen: list[httpx.Request] = []

    async def asyncSetUp(self):
        pty_session.manager.close_all()
        self.addCleanup(pty_session.manager.close_all)

    def _link(self, handler, token: str = "s3cret") -> RemoteLink:
        seen = self.seen

        async def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        return RemoteLink("http://terminal-host:3100", token=token,
                          transport=httpx.MockTransport(wrapped))

    async def test_the_token_travels_with_every_call(self):
        link = self._link(lambda _r: httpx.Response(200, json={"sessions": []}))
        await link.snapshot()
        self.assertEqual(self.seen[0].headers.get("X-Nova-Terminal-Token"), "s3cret")

    async def test_sessions_come_back_from_the_service(self):
        payload = {"active": "pty-1", "max_sessions": 8,
                   "sessions": [{"id": "pty-1", "label": "remote-tab"}]}
        link = self._link(lambda _r: httpx.Response(200, json=payload))
        self.assertEqual(await link.snapshot(), payload)
        self.assertEqual((await link.get("pty-1"))["label"], "remote-tab")
        self.assertIsNone(await link.get("pty-9"))
        self.assertEqual(self.seen[0].url.path, "/terminal/pty/sessions")

    async def test_a_remote_error_keeps_its_meaning(self):
        body = {"error": "the limit of 8 concurrent sessions is reached",
                "code": "session_limit"}
        link = self._link(lambda _r: httpx.Response(409, json=body))
        with self.assertRaises(SessionLimitReached) as err:
            await link.create()
        self.assertIn("concurrent sessions", str(err.exception))
        self.assertEqual(err.exception.status, 409)

    async def test_an_unreachable_service_says_so(self):
        def boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        link = self._link(boom)
        with self.assertRaises(LinkUnavailable) as err:
            await link.snapshot()
        self.assertIn("terminal-host:3100", str(err.exception))
        self.assertEqual(err.exception.status, 503)

    async def test_the_stream_is_forwarded_verbatim(self):
        sse = 'data: {"o": "hello"}\n\ndata: {"done": true, "exit": 0}\n\n'

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("/terminal/pty/stream", request.url.path)
            self.assertEqual(request.url.params["session"], "pty-1")
            return httpx.Response(200, text=sse,
                                  headers={"content-type": "text/event-stream"})

        link = self._link(handler)
        frames = [f async for f in link.stream("pty-1", 0, lambda: _false())]
        self.assertEqual("".join(frames), sse)

    async def test_the_agent_step_goes_to_the_remote_terminal(self):
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertEqual(request.url.path, "/terminal/pty/run")
            self.assertEqual(payload["command"], "echo remote-step")
            self.assertEqual(payload["label"], "agent · task")
            return httpx.Response(200, json={"ok": True, "output": "remote-step", "code": 0})

        link = self._link(handler)
        out, code = await link.run_command("echo remote-step", "agent · task", 30)
        self.assertEqual((out, code), ("remote-step", 0))

    async def test_a_gateway_restart_leaves_remote_shells_alone(self):
        # Shells must outlive the app, or every gateway deploy would kill the
        # shells the user is typing into.
        link = self._link(lambda _r: httpx.Response(200, json={}))
        await link.close_all()
        self.assertEqual(self.seen, [])


@_POSIX_ONLY
class SameRoutesBothHostsTests(unittest.TestCase):
    """The proof that the split didn't fork the API: the gateway's
    /api/admin/terminal/pty/* and the service's /terminal/pty/* are the same
    router, so a response proven against one is proven against the other."""

    def setUp(self):
        pty_session.manager.close_all()
        self.addCleanup(pty_session.manager.close_all)

    def test_both_mounts_answer_the_same_way(self):
        with TestClient(_app()) as service:
            created = service.post("/terminal/pty/sessions", json={"label": "both"})
            self.assertEqual(created.status_code, 200, created.text)
            session = created.json()["session"]

            listing = service.get("/terminal/pty/sessions").json()
            self.assertEqual(listing["active"], session)
            self.assertIn("both", [s["label"] for s in listing["sessions"]])

            # And the exact paths the dashboard's JS calls on the gateway.
            self.assertEqual(service.get("/terminal/pty/sessions").status_code, 200)
            self.assertEqual(service.post("/terminal/pty/input",
                                          json={"session": session, "data": "x"}).status_code,
                             200)
            self.assertEqual(service.post("/terminal/pty/rename",
                                          json={"session": session, "label": "y"}).status_code,
                             200)
            self.assertEqual(service.post("/terminal/pty/resize",
                                          json={"session": session, "cols": 80,
                                                "rows": 24}).status_code, 200)
            self.assertEqual(service.post("/terminal/pty/stop",
                                          json={"session": session}).status_code, 200)

    def test_a_dead_session_is_a_404_with_a_code(self):
        with TestClient(_app()) as service:
            res = service.post("/terminal/pty/input",
                               json={"session": "pty-gone", "data": "ls"})
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.json()["code"], "session_gone")

    def test_streaming_a_dead_session_is_a_404_not_an_empty_stream(self):
        # An empty 200 stream would hang the browser's EventSource forever
        # instead of showing "that session is gone".
        with TestClient(_app()) as service:
            res = service.get("/terminal/pty/stream", params={"session": "pty-gone"})
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.json()["code"], "session_gone")

    def test_the_agent_run_route_reports_output_and_code(self):
        with TestClient(_app()) as service:
            res = service.post("/terminal/pty/run",
                               json={"command": "echo route-marker", "timeout": 30})
            self.assertEqual(res.status_code, 200, res.text)
            body = res.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["code"], 0)
            self.assertIn("route-marker", body["output"])


class LinkSelectionTests(unittest.TestCase):
    """Which link a process uses is configuration, and the standalone service
    always owns its shells no matter what the environment says."""

    def setUp(self):
        self.addCleanup(terminal_link.pin, None)

    def test_no_url_means_in_process(self):
        terminal_link.pin(None)
        with unittest.mock.patch.object(terminal_link.config, "TERMINAL_SERVICE_URL", ""):
            self.assertFalse(terminal_link.is_remote())
            self.assertIsInstance(terminal_link.current(), LocalLink)

    def test_a_url_means_remote(self):
        terminal_link.pin(None)
        with unittest.mock.patch.object(
            terminal_link.config, "TERMINAL_SERVICE_URL", "http://terminal-host:3100"
        ):
            self.assertTrue(terminal_link.is_remote())
            self.assertIsInstance(terminal_link.current(), RemoteLink)

    def test_the_service_pins_its_own_shells(self):
        # Even with a URL in the environment, the service itself must not
        # proxy to a terminal — that is how a service ends up talking to
        # itself (or to a second copy of itself).
        terminal_link.pin(LocalLink())
        with unittest.mock.patch.object(
            terminal_link.config, "TERMINAL_SERVICE_URL", "http://127.0.0.1:3100"
        ):
            self.assertFalse(terminal_link.is_remote())


class AgentOverTheLinkTests(unittest.IsolatedAsyncioTestCase):
    """The agent's shell step must stay visible when the terminal is remote —
    that is the whole point of the split. If the link fails, the agent still
    falls back, but only then."""

    def setUp(self):
        pty_session.manager.close_all()
        self.addCleanup(pty_session.manager.close_all)
        self.addCleanup(terminal_link.pin, None)

    async def test_a_remote_step_is_reported_verbatim(self):
        from nova import agent

        class FakeRemote:
            remote = True

            def __init__(self):
                self.commands = []

            async def run_command(self, command, label=None, timeout=None):
                self.commands.append((command, label))
                return "remote-output", 0

        fake = FakeRemote()
        terminal_link.pin(fake)
        out = await agent.run_in_live_terminal("echo hi", label="agent · task")
        self.assertIn("remote-output", out)
        self.assertEqual(fake.commands, [("echo hi", "agent · task")])

    async def test_a_dead_link_falls_back_instead_of_raising(self):
        from nova import agent

        class DeadRemote:
            remote = True

            async def run_command(self, *_a, **_k):
                raise LinkUnavailable("terminal service is not answering")

        terminal_link.pin(DeadRemote())
        self.assertIsNone(await agent.run_in_live_terminal("echo hi"))


class StandaloneConsoleTests(unittest.TestCase):
    """A terminal hosted on its own has to be usable in a browser.

    The symptom this pins is a deployed terminal whose homepage answered
    {"detail": "Not Found"}: the host had every API route and no page at all.
    """

    @classmethod
    def setUpClass(cls):
        from terminal.service import app

        cls.client = TestClient(app)

    def test_the_root_serves_the_console(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.headers["content-type"].startswith("text/html"))
        # The chrome a terminal host has to offer on its own: session strip,
        # the draggable bar, zoom, expand, and the client that drives them.
        for needle in ('id="tabs"', 'id="split"', 'id="zoomIn"', 'id="expand"',
                       'class="mobile-switch"', "/static/console.js"):
            self.assertIn(needle, res.text, needle)

    def test_the_agent_url_is_the_same_console(self):
        # `/agent` is where the docs send people; it must not 404 either.
        self.assertEqual(self.client.get("/agent").status_code, 200)

    def test_the_client_is_served_and_readable(self):
        res = self.client.get("/static/console.js")
        self.assertEqual(res.status_code, 200)
        self.assertIn("application/javascript", res.headers["content-type"])
        self.assertIn("EventSource", res.text)

    def test_the_page_is_open_but_the_shells_are_not(self):
        self.assertEqual(self.client.get("/").status_code, 200)     # static html
        self.assertEqual(self.client.get("/terminal/pty/sessions").status_code,
                         200 if not service_module().TERMINAL_SERVICE_TOKEN else 401)

    def test_a_dead_session_says_so_instead_of_a_bare_404(self):
        # The console streams through this route; a blank stream would leave
        # the browser staring at an empty pane forever.
        with TestClient(_app()) as service:
            res = service.get("/terminal/pty/stream", params={"session": "pty-gone"})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["code"], "session_gone")


def service_module():
    """The service module, imported late so env patches in other tests apply."""
    from terminal import service

    return service


if __name__ == "__main__":
    unittest.main()
