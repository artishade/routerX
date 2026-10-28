"""Agentbox — the AI agent that ships with the terminal.

Run: python -m unittest discover -s tests -p test_agentbox.py

A separately hosted terminal is only useful if the user still gets the agent,
so Agentbox lives inside `terminal/` and talks to whatever model endpoint it was
configured with. What matters, and what is easy to get wrong:

  · a tool call has to land in a REAL terminal tab, not a hidden subprocess —
    that is the whole promise ("every command the agent runs appears in a tab")
  · an unconfigured host must say so, not fail with a stack trace or quietly
    pretend the agent exists
  · the terminal has to import and boot with no RouterX app at all, because
    a terminal is often deployed on its own
"""
import asyncio
import json
import os
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from terminal import agentbox
from terminal import pty as pty_session

ENDPOINT = "https://models.example/v1"


class _FakeModels(httpx.AsyncBaseTransport):
    """An OpenAI-compatible endpoint, driven by a scripted list of replies."""

    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.seen: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(json.loads(request.content or b"{}"))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "fake-model"}]})
        turn = self.replies.pop(0) if self.replies else _reply("done")
        return httpx.Response(200, json=turn)


class _DeadEndpoint(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")


def _fake_client(transport: httpx.AsyncBaseTransport):
    """Stand in for `agentbox._client`, routing the provider's own URL through
    the fake transport. The provider argument is real — that is what the tests
    are about."""
    def build(provider: "agentbox.Provider") -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=provider.base_url,
                                 headers={"Authorization": f"Bearer {provider.api_key}"},
                                 transport=transport)
    return build


def _reply(content: str = "") -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _tool_call(name: str, args: dict, call_id: str = "call-1") -> dict:
    return {"choices": [{"message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}}],
    }}]}


def _app() -> FastAPI:
    """The host, carrying only the agent routes (as the service mounts them)."""
    app = FastAPI()
    app.include_router(agentbox.router, prefix="/agent")
    return app


# A real shell tab needs a POSIX pseudo-terminal; `tests/test_portability.py`
# pins the typed 503 a host without one returns.
_POSIX_ONLY_RUN = unittest.skipUnless(pty_session.PTY_SUPPORTED, "interactive PTY sessions need a POSIX host")

# POSIX permission bits (`0o600`) are only meaningful on a POSIX filesystem:
# Windows `chmod` toggles the read-only flag only and always reports 0o666.
_POSIX_ONLY_MODE = unittest.skipUnless(os.name == "posix", "POSIX file modes only")


class AgentboxConfigTests(unittest.TestCase):
    """No endpoint configured means the agent is off — visibly."""

    def setUp(self):
        self._no_env()

    def _no_env(self):
        for name, value in (("AGENTBOX_BASE_URL", ""), ("AGENTBOX_API_KEY", ""),
                            ("AGENTBOX_MODEL", ""), ("AGENTBOX_PROVIDER_FILE", self._registry())):
            patcher = unittest.mock.patch.object(agentbox.config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._clear_registry)

    @staticmethod
    def _registry() -> str:
        return "/tmp/agentbox-test-providers.json"

    def _clear_registry(self):
        try:
            Path(self._registry()).unlink()
        except OSError:
            pass

    def test_an_unconfigured_host_says_so_instead_of_guessing(self):
        with TestClient(_app()) as client:
            res = client.post("/agent/chat", json={"message": "hello"})
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["code"], "agentbox_unconfigured")
        self.assertIn("NOVA_AGENTBOX_BASE_URL", res.json()["error"])

    def test_status_reports_the_truth_in_both_directions(self):
        status = agentbox.agentbox_status()
        self.assertFalse(status["configured"])
        self.assertIsNone(status["endpoint"])
        self.assertEqual(status["providers"], [])

        agentbox.upsert_provider(id="groq", base_url="https://api.groq.com/openai/v1",
                                 api_key="sk-groq-secret", model="llama-3.3-70b")
        status = agentbox.agentbox_status()
        self.assertTrue(status["configured"])
        self.assertEqual(status["active"], "groq")
        self.assertEqual(status["providers"], ["groq"])

    def test_a_missing_message_is_a_400_not_a_thought(self):
        agentbox.upsert_provider(id="groq", base_url="https://api.groq.com/openai/v1")
        with TestClient(_app()) as client:
            res = client.post("/agent/chat", json={"message": "   "})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "bad_request")


class AgentboxProviderTests(unittest.TestCase):
    """Custom providers: added at runtime, kept private, and chosen per chat."""

    def setUp(self):
        registry = "/tmp/agentbox-test-providers.json"
        Path(registry).unlink(missing_ok=True)
        for name, value in (("AGENTBOX_BASE_URL", ""), ("AGENTBOX_API_KEY", ""),
                            ("AGENTBOX_MODEL", ""), ("AGENTBOX_PROVIDER_FILE", registry)):
            patcher = unittest.mock.patch.object(agentbox.config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: Path(registry).unlink(missing_ok=True))

    def _add(self, pid="groq", url="https://api.groq.com/openai/v1", key="sk-live-secret-1234",
             model="llama-3.3-70b", label="Groq"):
        return agentbox.upsert_provider(id=pid, base_url=url, api_key=key,
                                       model=model, label=label)

    def test_a_custom_provider_can_be_added_listed_and_chosen(self):
        self._add()
        self._add("local", url="http://127.0.0.1:8000/v1", key="", model="qwen2.5")

        with TestClient(_app()) as client:
            listing = client.get("/agent/providers").json()
            self.assertEqual([p["id"] for p in listing["providers"]], ["groq", "local"])
            self.assertEqual(listing["active"], "groq")   # first saved wins

            fake = _FakeModels([_reply("hi")])
            with unittest.mock.patch.object(agentbox, "_client", _fake_client(fake)):
                chat = client.post("/agent/chat", json={"message": "hi", "provider": "local"})
        # `local` has no key and its own model — the point of a custom provider.
        self.assertEqual(chat.json()["provider"], "local")
        self.assertEqual(chat.json()["model"], "qwen2.5")
        self.assertEqual(fake.seen[-1]["model"], "qwen2.5")

    def test_a_key_is_never_echoed_back(self):
        self._add()
        with TestClient(_app()) as client:
            body = client.get("/agent/providers").json()
        blob = json.dumps(body)
        self.assertNotIn("sk-live-secret-1234", blob)
        self.assertTrue(body["providers"][0]["has_key"])
        self.assertIn("…", body["providers"][0]["api_key"])

    def test_editing_a_provider_without_its_key_keeps_the_stored_one(self):
        self._add()
        self._add(model="llama-3.1-8b")     # no key in this edit
        stored = agentbox.load_providers()["groq"]
        self.assertEqual(stored.api_key, "sk-live-secret-1234")
        self.assertEqual(stored.model, "llama-3.1-8b")

    def test_a_provider_can_be_removed(self):
        self._add()
        self._add("local", url="http://127.0.0.1:8000/v1")
        with TestClient(_app()) as client:
            self.assertEqual(client.delete("/agent/providers/local").status_code, 200)
            self.assertEqual(client.delete("/agent/providers/local").status_code, 404)
            self.assertEqual([p["id"] for p in client.get("/agent/providers").json()["providers"]],
                             ["groq"])

    def test_the_environment_provider_cannot_be_deleted_from_the_api(self):
        patcher = unittest.mock.patch.object(agentbox.config, "AGENTBOX_BASE_URL", ENDPOINT)
        patcher.start()
        self.addCleanup(patcher.stop)
        with TestClient(_app()) as client:
            res = client.delete("/agent/providers/env")
        self.assertEqual(res.status_code, 400)
        self.assertIn("environment", res.json()["error"])

    def test_the_environment_provider_is_the_default_when_present(self):
        patcher = unittest.mock.patch.object(agentbox.config, "AGENTBOX_BASE_URL", ENDPOINT)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._add()
        self.assertEqual(agentbox.agentbox_status()["active"], "env")

    def test_nonsense_is_rejected_with_an_explanation(self):
        with TestClient(_app()) as client:
            bad_id = client.post("/agent/providers",
                                 json={"id": "Not An Id", "base_url": "https://x.test/v1"})
            bad_url = client.post("/agent/providers",
                                  json={"id": "x", "base_url": "x.test/v1"})
            # Nothing configured at all is a 503, not a 404 about one name.
            unconfigured = client.post("/agent/chat",
                                       json={"message": "hi", "provider": "ghost"})
        self.assertEqual(bad_id.status_code, 400)
        self.assertIn("id must be", bad_id.json()["error"])
        self.assertEqual(bad_url.status_code, 400)
        self.assertIn("http://", bad_url.json()["error"])
        self.assertEqual(unconfigured.status_code, 503)

        self._add()
        with TestClient(_app()) as client:
            missing = client.post("/agent/chat", json={"message": "hi", "provider": "ghost"})
        # A provider that does not exist lists the ones that do.
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["code"], "provider_not_found")
        self.assertIn("groq", missing.json()["error"])

    @_POSIX_ONLY_MODE
    def test_the_registry_file_is_private(self):
        self._add()
        mode = Path(agentbox.registry_path()).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


@_POSIX_ONLY_RUN
class AgentboxRunTests(unittest.IsolatedAsyncioTestCase):
    """A tool call must run in a real tab, and the answer must come back."""

    def setUp(self):
        pty_session.manager.close_all()
        self.addCleanup(pty_session.manager.close_all)
        registry = "/tmp/agentbox-run-providers.json"
        Path(registry).unlink(missing_ok=True)
        for name, value in (("AGENTBOX_BASE_URL", ENDPOINT), ("AGENTBOX_API_KEY", "test"),
                            ("AGENTBOX_MODEL", "fake-model"),
                            ("AGENTBOX_PROVIDER_FILE", registry)):
            patcher = unittest.mock.patch.object(agentbox.config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: Path(registry).unlink(missing_ok=True))

    @staticmethod
    def _run(transport: httpx.AsyncBaseTransport, message: str = "echo hi", **extra) -> dict:
        """Drive the real chat route against a fake model endpoint."""
        with unittest.mock.patch.object(agentbox, "_client", _fake_client(transport)), \
             TestClient(_app()) as client:
            res = client.post("/agent/chat", json={"message": message, **extra})
        return {"status": res.status_code, "body": res.json()}

    async def test_a_command_lands_in_a_terminal_tab_the_user_can_see(self):
        model = _FakeModels([
            _tool_call("run_command", {"command": "echo agentbox-was-here"}, "c1"),
            _reply("The shell says agentbox-was-here."),
        ])
        got = await asyncio.to_thread(self._run, model)
        self.assertEqual(got["status"], 200, got["body"])
        self.assertEqual(got["body"]["reply"], "The shell says agentbox-was-here.")

        step = got["body"]["steps"][0]
        self.assertEqual(step["tool"], "run_command")
        self.assertEqual(step["result"]["exit_code"], 0)
        self.assertIn("agentbox-was-here", step["result"]["output"])

        # The promise that matters: a tab labelled for the task exists, holding
        # the same output the agent got back.
        tabs = [s["label"] for s in pty_session.manager.list_sessions()]
        self.assertTrue(any(label.startswith("agent · ") for label in tabs), tabs)

    async def test_the_tool_result_is_handed_back_to_the_model(self):
        model = _FakeModels([
            _tool_call("run_command", {"command": "echo round-trip"}, "c1"),
            _reply("ok"),
        ])
        await asyncio.to_thread(self._run, model)
        # The second turn must carry the tool output, or the model is guessing.
        tool_msgs = [m for m in model.seen[-1]["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("round-trip", tool_msgs[0]["content"])
        self.assertEqual(tool_msgs[0]["tool_call_id"], "c1")

    async def test_write_then_read_round_trips_through_the_workspace(self):
        workspace = Path("/tmp/agentbox-workspace")
        workspace.mkdir(parents=True, exist_ok=True)
        target = workspace / "note.txt"
        self.addCleanup(target.unlink, True)
        model = _FakeModels([
            _tool_call("write_file", {"path": str(target), "content": "kept"}, "w"),
            _reply("written."),
        ])
        got = await asyncio.to_thread(self._run, model)
        self.assertEqual(got["status"], 200, got["body"])
        self.assertTrue(got["body"]["steps"][0]["result"]["ok"])
        self.assertEqual(target.read_text(), "kept")

        read_back = await agentbox.run_tool("read_file", {"path": str(target)}, "agent · test")
        self.assertIn("kept", read_back["content"])

    async def test_finish_ends_the_task_with_its_own_summary(self):
        model = _FakeModels([
            _tool_call("finish", {"summary": "Deployed and verified."}, "f"),
            _tool_call("run_command", {"command": "echo never"}, "nope"),
        ])
        got = await asyncio.to_thread(self._run, model)
        self.assertEqual(got["body"]["reply"], "Deployed and verified.")
        self.assertEqual(len(got["body"]["steps"]), 1)   # stopped on finish

    async def test_a_broken_tool_call_does_not_kill_the_run(self):
        model = _FakeModels([
            _tool_call("run_command", {"command": ""}, "c1"),   # empty → error
            _reply("That command was empty, so I stopped."),
        ])
        got = await asyncio.to_thread(self._run, model)
        self.assertEqual(got["status"], 200, got["body"])
        self.assertIn("error", got["body"]["steps"][0]["result"])
        self.assertEqual(got["body"]["reply"], "That command was empty, so I stopped.")

    async def test_an_unreachable_endpoint_is_reported_not_swallowed(self):
        got = await asyncio.to_thread(self._run, _DeadEndpoint())
        self.assertEqual(got["status"], 502)
        self.assertEqual(got["body"]["code"], "agentbox_unreachable")

    async def test_a_per_message_model_overrides_the_provider_default(self):
        model = _FakeModels([_reply("ok")])
        got = await asyncio.to_thread(self._run, model, model="other-model")
        self.assertEqual(got["body"]["model"], "other-model")
        self.assertEqual(model.seen[-1]["model"], "other-model")


class TerminalIsStandaloneTests(unittest.TestCase):
    """`terminal/` must import and work with no RouterX app present — that
    is what makes "host only the terminal" a deployment, not a fork."""

    HOST_MODULES = ["__init__.py", "config.py", "pty.py", "link.py",
                    "api.py", "service.py", "agentbox.py"]

    def test_nothing_in_the_host_path_imports_the_app(self):
        root = Path(__file__).resolve().parent.parent / "terminal"
        for name in self.HOST_MODULES:
            source = (root / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertNotIn("import nova", source)
                self.assertNotIn("from nova", source)
                self.assertNotIn("nova.", source)

    def test_the_package_imports_with_nova_blocked(self):
        # Python 3.12 dropped the legacy find_module/load_module protocol, so
        # block `nova` through find_spec (raising ImportError from a meta path
        # finder swallows the import exactly like a missing module).
        script = (
            "import sys\n"
            "class Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] == 'nova': raise ImportError(name)\n"
            "        return None\n"
            "sys.meta_path.insert(0, Block())\n"
            "import terminal, terminal.service, terminal.agentbox, terminal.sandbox\n"
            "assert terminal.sandbox.APP_AVAILABLE is False\n"
            "print('standalone-ok')\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=120,
        )
        self.assertIn("standalone-ok", out.stdout, out.stderr)

    def test_the_sandbox_reports_a_missing_gateway_instead_of_crashing(self):
        import terminal.sandbox as sandbox

        with unittest.mock.patch.object(sandbox, "APP_AVAILABLE", False):
            result = sandbox.execute_command(None, "pwd")
        self.assertEqual(result["code"], 1)
        self.assertIn("not attached", result["stderr"])


if __name__ == "__main__":
    unittest.main()