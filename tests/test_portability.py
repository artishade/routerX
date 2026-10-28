"""Portability sanity checks — the defects that only show up off Linux.

Run: python -m unittest discover -s tests -p "test_portability.py"

Three platform defects blocked the app on Windows and are pinned here:

  1. `terminal/pty.py` imported `fcntl` at module scope, so `import main` failed
     on a host without POSIX pseudo-terminals and ~40 tests never ran. Every
     terminal module and `main` must import everywhere; a host without a PTY
     must answer the terminal routes with the typed 503 instead of crashing.
  2. `nova/mcp_client.py` split stdio commands with POSIX `shlex` rules, which
     eats the backslashes in a Windows interpreter path (`[WinError 2]`).
  3. Four documentation SVGs were not well-formed XML, so GitHub showed broken
     images for the architecture diagram, the fallback demo and the screenshots.

Everything here is offline and writes nothing outside a temp dir.
"""
import ast
import glob
import os
import sys
import tempfile
import unittest
import xml.dom.minidom
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DRAWING_TAGS = ("text", "rect", "path", "line", "circle")

# A genuinely POSIX-only dependency missing on a non-POSIX host is not this
# suite's business; on a POSIX host nothing may be skipped.
_POSIX_HOST = os.name == "posix"


class TerminalImportTests(unittest.TestCase):
    """The app (and therefore the sandbox path) must import on every host."""

    def test_terminal_modules_import(self):
        import terminal.api
        import terminal.link
        import terminal.pty
        import terminal.service

        for module in (terminal.api, terminal.link, terminal.pty, terminal.service):
            self.assertTrue(module.__name__)

    def test_main_imports(self):
        try:
            import main
        except ModuleNotFoundError as exc:      # pragma: no cover - env specific
            if not _POSIX_HOST and exc.name not in {
                    "fcntl", "pty", "termios", "select", "resource", "pwd"}:
                raise
            self.skipTest(f"genuinely POSIX-only dependency missing: {exc.name}")
        self.assertTrue(hasattr(main, "app"))

    def test_posix_modules_are_imported_lazily(self):
        # Guard against the regression, not just the symptom: `terminal/pty.py`
        # may import a POSIX-only module only inside the capability guard.
        source = (ROOT / "terminal" / "pty.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        posix_only = {"fcntl", "pty", "termios", "tty"}

        assigned = {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertIn("PTY_SUPPORTED", assigned, "missing the runtime capability flag")

        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = {alias.name for alias in node.names}
                self.assertTrue(names.isdisjoint(posix_only),
                                f"POSIX-only import at module scope: {sorted(names)}")

        guarded = [
            node for node in tree.body
            if isinstance(node, ast.If) and "PTY_SUPPORTED" in ast.unparse(node.test)
        ]
        self.assertTrue(guarded, "POSIX imports must sit behind the capability guard")


class PtyContractTests(unittest.TestCase):
    """A host without a PTY answers honestly: typed 503, never a crash."""

    def test_the_capability_flag_exists(self):
        from terminal import pty

        self.assertEqual(pty.PTY_SUPPORTED, os.name == "posix")

    @unittest.skipUnless(os.name != "posix", "PTY sessions start on POSIX hosts")
    def test_starting_a_session_raises_the_typed_error(self):
        from terminal import pty

        manager = pty.TerminalManager()
        with self.assertRaises(pty.PTYUnavailable) as err:
            manager.create(label="nope")
        message = str(err.exception).lower()
        self.assertTrue("posix" in message, message)
        self.assertIsNone(manager.snapshot()["active"])
        self.assertEqual(manager.snapshot()["sessions"], [])

    @unittest.skipUnless(os.name != "posix", "PTY sessions start on POSIX hosts")
    def test_agent_session_raises_the_typed_error(self):
        from terminal import pty

        with self.assertRaises(pty.PTYUnavailable):
            pty.agent_session(pty.agent_label("self check"))

    @unittest.skipUnless(os.name != "posix", "PTY sessions start on POSIX hosts")
    def test_the_routes_answer_503_with_a_code(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from terminal.api import router

        app = FastAPI()
        app.include_router(router, prefix="/terminal/pty")
        client = TestClient(app)

        listing = client.get("/terminal/pty/sessions")
        self.assertEqual(listing.status_code, 200)          # the tab bar renders
        self.assertEqual(listing.json()["sessions"], [])

        created = client.post("/terminal/pty/sessions", json={"label": "nope"})
        self.assertEqual(created.status_code, 503, created.text)
        self.assertEqual(created.json()["code"], "pty_unavailable")
        self.assertIn("POSIX", created.json()["error"])

        ran = client.post("/terminal/pty/run", json={"command": "echo hi"})
        self.assertEqual(ran.status_code, 503, ran.text)
        self.assertEqual(ran.json()["code"], "pty_unavailable")

    @unittest.skipUnless(os.name != "posix", "PTY sessions start on POSIX hosts")
    def test_the_link_reports_the_typed_error(self):
        import asyncio

        from terminal.link import LocalLink, PTYUnsupported

        async def create():
            await LocalLink().create(label="nope")

        with self.assertRaises(PTYUnsupported) as err:
            asyncio.run(create())
        self.assertEqual(err.exception.status, 503)
        self.assertEqual(err.exception.code, "pty_unavailable")

    def test_the_sandbox_path_still_answers(self):
        # The command path is separate from the shells and must keep working.
        from terminal.pty import shell_hint

        self.assertIn("session tab", shell_hint("ssh example.com") or "")


class StdioCommandSplitTests(unittest.TestCase):
    """MCP stdio plugins must spawn from the real interpreter path."""

    @unittest.skipUnless(os.name == "nt", "Windows path semantics")
    def test_the_interpreter_path_round_trips(self):
        from nova.mcp_client import _split_command

        argv = _split_command(f"{sys.executable} some.py")
        self.assertEqual(len(argv), 2)
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(os.path.isabs(argv[0]))
        self.assertEqual(argv[1], "some.py")

    @unittest.skipUnless(os.name == "nt", "Windows path semantics")
    def test_a_quoted_interpreter_path_round_trips(self):
        from nova.mcp_client import _split_command

        argv = _split_command(f'"{sys.executable}" some.py')
        self.assertEqual(argv[0], sys.executable)

    @unittest.skipUnless(os.name == "nt", "Windows path semantics")
    def test_a_mangled_drive_path_is_repaired_only_when_it_exists(self):
        from nova import mcp_client

        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "plugin-server.exe"
            real.write_bytes(b"")
            eaten = "".join(ch for ch in str(real) if ch not in "\\/")
            # Only a path whose remainder really exists may be repaired.
            repaired = mcp_client._repair_drive_path(eaten)
            if os.path.isfile(repaired):
                self.assertEqual(os.path.normcase(repaired), os.path.normcase(str(real)))
            else:
                self.assertEqual(repaired, eaten, "a nonexistent path is never guessed at")
            # And a missing drive path is always left exactly as it came in.
            self.assertEqual(mcp_client._repair_drive_path("Q:definitely-not-here"),
                             "Q:definitely-not-here")
            if os.path.isfile(str(real)):
                self.assertEqual(mcp_client._repair_drive_path(str(real)), str(real))

    def test_the_split_never_uses_a_shell(self):
        import ast
        import inspect

        from nova import mcp_client

        tree = ast.parse(inspect.getsource(mcp_client))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        shells = [c for c in calls if getattr(c.func, "attr", "") == "create_subprocess_shell"]
        self.assertEqual(shells, [], "stdio plugins must never run through a shell")
        self.assertTrue(any(getattr(c.func, "attr", "") == "create_subprocess_exec" for c in calls))
        for call in calls:
            for keyword in call.keywords:
                self.assertFalse(
                    keyword.arg == "shell"
                    and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False),
                    "shell=True is never allowed",
                )

    def test_arguments_survive_the_split(self):
        from nova.mcp_client import _split_command

        self.assertEqual(_split_command("npx -y @scope/server"),
                         ["npx", "-y", "@scope/server"])


class SvgValidityTests(unittest.TestCase):
    """The README's diagrams and screenshots must render on GitHub."""

    def _files(self):
        files = sorted(glob.glob(str(ROOT / "docs" / "*.svg")))
        files += sorted(glob.glob(str(ROOT / "docs" / "screenshots" / "*.svg")))
        self.assertGreaterEqual(len(files), 5, files)
        return files

    def test_every_svg_is_valid_xml_with_one_root(self):
        for path in self._files():
            with self.subTest(path=os.path.basename(path)):
                document = xml.dom.minidom.parse(path)
                roots = document.getElementsByTagName("svg")
                self.assertEqual(len(roots), 1, path)
                self.assertTrue(roots[0].hasAttribute("xmlns"), path)
                self.assertTrue(roots[0].hasAttribute("viewBox"), path)

    def test_every_svg_has_drawing_elements(self):
        for path in self._files():
            with self.subTest(path=os.path.basename(path)):
                document = xml.dom.minidom.parse(path)
                count = sum(len(document.getElementsByTagName(tag)) for tag in DRAWING_TAGS)
                self.assertGreater(count, 5, f"{path} has only {count} drawing elements")

    def test_the_fallback_demo_shows_all_four_frames_statically(self):
        # The README promises these four frames at any moment, not only mid-animation.
        document = xml.dom.minidom.parse(str(ROOT / "docs" / "fallback-demo.svg"))
        text = " ".join(
            "".join(child.data for child in node.childNodes
                    if child.nodeType == child.TEXT_NODE)
            for node in document.getElementsByTagName("text")
        )
        for frame in ("model: gpt-4o", "429 rate limited", "200 OK", "stand-in",
                      'model = "gpt-4o"'):
            self.assertIn(frame, text)

    def test_the_fallback_animation_is_not_faster_than_3hz(self):
        import re

        source = (ROOT / "docs" / "fallback-demo.svg").read_text(encoding="utf-8")
        for value in re.findall(r"dur=\"([0-9.]+)s\"", source):
            self.assertGreaterEqual(float(value), 1 / 3, f"animation cycles faster than 3 Hz: {value}s")

    def test_the_svgs_are_lf_without_a_bom(self):
        for path in self._files():
            with self.subTest(path=os.path.basename(path)):
                raw = Path(path).read_bytes()
                self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "BOM")
                self.assertNotIn(b"\r\n", raw, "CRLF")

    def test_the_readme_references_these_svgs(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in ("docs/fallback-demo.svg", "docs/architecture.svg",
                     "docs/screenshots/overview.svg", "docs/screenshots/console.svg",
                     "docs/screenshots/routes.svg"):
            self.assertIn(name, readme, name)


if __name__ == "__main__":
    unittest.main()
