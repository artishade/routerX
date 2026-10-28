"""Agent shell steps run in the terminal you can watch.

Run: python -m unittest discover -s tests -p test_agent_terminal_session.py

The autonomous agent used to run its shell steps in a throwaway subprocess,
so a task typed into the workspace chatbox produced no visible activity in the
terminal. `terminal.pty.agent_session` + `run_in_session` give the agent a
real, labelled PTY tab instead; these tests pin the parts that are easy to get
wrong (the PTY echoes what we type, the shell keeps state between commands,
exit codes have to survive the round trip).
"""
import asyncio
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terminal import pty

# The real interactive terminal is a POSIX feature (`terminal.pty`): these
# tests open real shells, so they are skipped on a host without a PTY rather
# than failing on platform. `tests/test_portability.py` pins that contract.
_POSIX_ONLY = unittest.skipUnless(pty.PTY_SUPPORTED, "interactive PTY sessions need a POSIX host")


@_POSIX_ONLY
class AgentSessionTests(unittest.TestCase):
    def setUp(self):
        pty.manager.close_all()
        self.addCleanup(pty.manager.close_all)

    def test_agent_session_is_labelled_and_reused(self):
        first = pty.agent_session()
        self.assertIsNotNone(first)
        self.assertEqual(first.label, pty.AGENT_SESSION_LABEL)
        self.assertFalse(first.closed)
        # A second task must land in the same tab, not pile up new shells.
        self.assertIs(pty.agent_session(), first)
        # And the tab has to be visible to the workspace client, which looks
        # the session up by label.
        labels = [s["label"] for s in pty.manager.list_sessions()]
        self.assertIn(pty.AGENT_SESSION_LABEL, labels)

    def test_run_returns_output_not_the_typed_command(self):
        session = pty.agent_session()
        out, code = pty.run_in_session(session, "echo nova-agent-marker", timeout=30)
        self.assertEqual(code, 0)
        self.assertIn("nova-agent-marker", out)
        # The tty echoes every byte we write; the agent must only see output.
        self.assertNotIn("printf", out)
        self.assertNotIn("NOVA_DONE_", out)

    def test_exit_code_survives_the_round_trip(self):
        session = pty.agent_session()
        _, ok = pty.run_in_session(session, "true", timeout=30)
        self.assertEqual(ok, 0)
        _, bad = pty.run_in_session(session, "false", timeout=30)
        self.assertEqual(bad, 1)
        out, missing = pty.run_in_session(session, "definitely-not-a-command-xyz", timeout=30)
        self.assertNotEqual(missing, 0)
        self.assertIn("not found", out)

    def test_shell_state_persists_between_agent_steps(self):
        session = pty.agent_session()
        pty.run_in_session(session, "cd /tmp", timeout=30)
        out, code = pty.run_in_session(session, "pwd", timeout=30)
        self.assertEqual(code, 0)
        self.assertIn("/tmp", out)
        # …and OSC7 cwd tracking keeps the tab label honest about it.
        self.assertEqual(session.cwd, "/tmp")

    def test_timeout_is_reported_honestly(self):
        session = pty.agent_session()
        out, code = pty.run_in_session(session, "sleep 30", timeout=1)
        self.assertEqual(code, -1)
        self.assertIsInstance(out, str)

    def test_ansi_is_stripped_from_the_tool_result(self):
        session = pty.agent_session()
        out, _ = pty.run_in_session(session, "printf '\\033[31mred\\033[0m'", timeout=30)
        self.assertIn("red", out)
        self.assertNotIn("\x1b[31m", out)

    def test_output_without_a_trailing_newline_stays_complete(self):
        # No final newline means the next prompt lands on the output's own line,
        # so the cleaner has to cut the prompt instead of the whole line.
        session = pty.agent_session()
        out, code = pty.run_in_session(
            session, "printf 'no-newline-tail'", timeout=30)
        self.assertEqual(code, 0)
        self.assertEqual(out, "no-newline-tail")

    def test_multiline_and_banner_do_not_leak_into_the_result(self):
        session = pty.agent_session()
        pty.run_in_session(session, "true", timeout=30)
        out, code = pty.run_in_session(
            session, "printf 'one\\ntwo\\nthree\\n'", timeout=30)
        self.assertEqual(code, 0)
        self.assertEqual(out, "one\ntwo\nthree")
        # The banner the login shell prints belongs to the first command only.
        self.assertNotIn("RouterX Cloud Shell", out)


    def test_each_task_gets_its_own_tab(self):
        # Two different goals must not share one shell, otherwise the second
        # task inherits the first task's cwd, venv and environment.
        first = pty.agent_session(pty.agent_label("audit the build log"))
        second = pty.agent_session(pty.agent_label("delete old uploads"))
        self.assertNotEqual(first.id, second.id)
        self.assertTrue(pty.is_agent_label(first.label))
        self.assertTrue(pty.is_agent_label(second.label))
        self.assertIn("audit the build", first.label)
        # ...but the steps of one task have to keep hitting the same shell.
        self.assertIs(pty.agent_session(first.label), first)
        labels = [s["label"] for s in pty.manager.list_sessions()]
        self.assertIn(first.label, labels)
        self.assertIn(second.label, labels)

    def test_label_identifies_the_task_but_is_readable(self):
        label = pty.agent_label("Build  and ship   the site\nnow")
        self.assertTrue(label.startswith("agent · "))
        self.assertIn("Build and ship the site", label)
        self.assertLessEqual(len(label), len("agent · ") + 28)
        self.assertEqual(pty.agent_label(""), pty.AGENT_SESSION_LABEL)
        self.assertFalse(pty.is_agent_label("Root@Build"))

    def test_state_does_not_leak_between_tasks(self):
        first = pty.agent_session(pty.agent_label("task one"))
        pty.run_in_session(first, "cd /tmp", timeout=30)
        second = pty.agent_session(pty.agent_label("task two"))
        out, code = pty.run_in_session(second, "pwd", timeout=30)
        self.assertEqual(code, 0)
        # A fresh shell starts in the configured build root (/app/build in the
        # image, a package-local dir for an unprivileged local run), not /tmp.
        from terminal.config import build_root
        self.assertIn(str(build_root()), out)
        self.assertEqual(pty.agent_session(first.label), first)


@_POSIX_ONLY
class AgentToolRoutingTests(unittest.IsolatedAsyncioTestCase):
    """The agent's tools must prefer the live session and still fall back."""

    def setUp(self):
        pty.manager.close_all()
        self.addCleanup(pty.manager.close_all)

    async def test_bash_exec_runs_in_the_live_session(self):
        from nova import agent

        out = await agent.tool_bash_exec("echo routed-to-pty; pwd")
        self.assertIn("routed-to-pty", out)
        self.assertIn("agent", [s["label"] for s in pty.manager.list_sessions()])

    async def test_tool_uses_the_task_tab_not_an_older_one(self):
        from nova import agent

        await agent.tool_terminal("echo first-task")
        await agent.tool_terminal("echo second-task")
        agent_tabs = [s for s in pty.manager.list_sessions()
                      if pty.is_agent_label(s["label"])]
        # No label set (a bare tool call outside a task) → still one shared tab.
        self.assertEqual(len(agent_tabs), 1)

        agent._ACTIVE_AGENT_LABEL = pty.agent_label("clean up temp files")
        try:
            await agent.tool_terminal("echo in-the-task-tab")
        finally:
            agent._ACTIVE_AGENT_LABEL = None
        labels = [s["label"] for s in pty.manager.list_sessions()]
        self.assertTrue(any("clean up temp files" in x for x in labels), labels)

    async def test_terminal_runs_in_the_live_session(self):
        from nova import agent

        import os
        import pwd
        out = await agent.tool_terminal("whoami")
        # The live shell runs as the server's own user (root in the image,
        # the invoking user for an unprivileged local run).
        self.assertIn(pwd.getpwuid(os.geteuid()).pw_name, out)
        self.assertIn("agent", [s["label"] for s in pty.manager.list_sessions()])

    async def test_falls_back_when_no_session_can_be_had(self):
        from nova import agent

        async def boom(command, timeout=45):
            return None

        with unittest.mock.patch.object(agent, "run_in_live_terminal", boom):
            calls = []

            def fake_bash(cmd, timeout=30):
                calls.append(cmd)
                return {"output": f"$ {cmd}\nsubprocess-fallback"}

            with unittest.mock.patch("terminal.sandbox.builder_bash", fake_bash):
                out = await agent.tool_bash_exec("echo fallback")
        self.assertEqual(calls, ["echo fallback"])
        self.assertIn("subprocess-fallback", out)


if __name__ == "__main__":
    unittest.main()
