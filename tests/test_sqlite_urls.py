"""SQLite DATABASE_URL handling - normalization and filesystem paths.

Offline and cross-platform: every form is asserted for the *running* platform
(`os.name == "nt"` branches where Windows and POSIX genuinely differ), plus the
round-trip property `sqlite_file_path(normalize(x)) == sqlite_file_path(x)`.
`ensure_sqlite_dir()` is exercised through a temp directory outside the repo
and through a monkeypatched `DATABASE_URL`; nothing is written to the repo.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova import database as nova_database
from nova.config import normalize_database_url
from nova.database import ensure_sqlite_dir, sqlite_file_path

WINDOWS = os.name == "nt"


def _urls():
    """(raw url, unnormalized file path expectation, normalized url)."""
    # A drive letter is a real path on Windows and a plain relative name on
    # POSIX; a four-slash path is native absolute on POSIX and has no root on
    # Windows, so each keeps its own expectation.
    drive = "C:/x.db"
    posix_abs = "\\abs\\posix.db" if WINDOWS else "/abs/posix.db"
    return [
        ("sqlite:///C:/x.db", drive, "sqlite:///C:/x.db"),
        ("sqlite:///C:\\x.db", drive, "sqlite:///C:\\x.db"),
        ("sqlite:////C:/x.db", drive, "sqlite:///C:/x.db"),
        ("sqlite:///relative.db", "relative.db", "sqlite:///relative.db"),
        ("sqlite:////abs/posix.db", posix_abs, "sqlite:////abs/posix.db"),
        ("sqlite:///./relative.db", "./relative.db", "sqlite:///./relative.db"),
        ("sqlite:///C:/x.db?cache=shared", drive, "sqlite:///C:/x.db?cache=shared"),
        ("sqlite:////abs/posix.db?cache=shared", posix_abs, "sqlite:////abs/posix.db?cache=shared"),
        # libpq style (one slash, no netloc) and a host-less relative URL.
        ("sqlite:/C:/x.db", drive, "sqlite:///C:/x.db"),
        ("sqlite://rel.db", "rel.db", "sqlite:///rel.db"),
    ]


class SqliteFilePathTests(unittest.TestCase):
    def test_every_accepted_form_resolves_to_the_native_path(self):
        for raw, expected, _normalized in _urls():
            with self.subTest(url=raw):
                self.assertEqual(sqlite_file_path(raw), expected)

    def test_windows_drive_path_never_gains_a_leading_separator(self):
        for raw in ("sqlite:///C:/x.db", "sqlite:////C:/x.db", "sqlite:///C:\\x.db",
                    "sqlite:/C:/x.db"):
            with self.subTest(url=raw):
                path = sqlite_file_path(raw)
                self.assertFalse(path.startswith(("/", "\\", "\\\\")), path)

    def test_blank_and_in_memory_urls_have_no_file(self):
        for raw in ("sqlite://", "sqlite:///:memory:", "sqlite:////:memory:",
                    "postgresql://u:p@h/db", ""):
            with self.subTest(url=raw):
                self.assertIsNone(sqlite_file_path(raw))

    def test_posix_absolute_keeps_exactly_one_root(self):
        path = sqlite_file_path("sqlite:////abs/x.db")
        if WINDOWS:
            self.assertEqual(path, "\\abs\\x.db")
        else:
            self.assertTrue(path.startswith("/"), path)
            self.assertFalse(path.startswith("//"), path)
            self.assertEqual(path, "/abs/x.db")


class NormalizeDatabaseUrlTests(unittest.TestCase):
    def test_normalize_keeps_the_slash_shape(self):
        for raw, _path, expected in _urls():
            with self.subTest(url=raw):
                self.assertEqual(normalize_database_url(raw), expected)

    def test_round_trip_preserves_the_file_path(self):
        for raw, _path, _expected in _urls():
            with self.subTest(url=raw):
                self.assertEqual(sqlite_file_path(normalize_database_url(raw)),
                                 sqlite_file_path(raw))

    def test_normalize_is_idempotent(self):
        for raw, _path, _expected in _urls():
            with self.subTest(url=raw):
                once = normalize_database_url(raw)
                self.assertEqual(normalize_database_url(once), once)

    def test_prisma_file_form_with_a_windows_drive_survives(self):
        path = sqlite_file_path(normalize_database_url("file:C:/db/x.db"))
        self.assertEqual(path, "C:/db/x.db")
        # A relative Prisma path is resolved against the project root.
        custom = sqlite_file_path(normalize_database_url("file:./db/custom.db"))
        self.assertTrue(custom.replace("\\", "/").endswith("db/custom.db"), custom)


class EnsureSqliteDirTests(unittest.TestCase):
    def _with_url(self, url):
        return patch.object(nova_database, "DATABASE_URL", url)

    def test_creates_a_nested_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "deeper" / "boot.db"
            with self._with_url(f"sqlite:///{target.as_posix()}"):
                self.assertTrue(ensure_sqlite_dir())
            self.assertTrue(target.parent.is_dir())
            self.assertFalse(target.exists())

    def test_impossible_path_logs_a_warning_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The parent is a regular file, so mkdir can never create the dir.
            blocker = Path(tmp) / "not-a-dir"
            blocker.write_text("x", encoding="utf-8")
            impossible = f"sqlite:///{(blocker / 'sub' / 'boot.db').as_posix()}"
            with self._with_url(impossible), self.assertLogs("nova.database", level="WARNING"):
                self.assertFalse(ensure_sqlite_dir())

    def test_drive_root_and_bare_filename_have_no_directory_to_create(self):
        if WINDOWS:
            # `C:/` is the drive root: mkdir on it is the old WinError 123.
            self.assertEqual(os.path.dirname("C:/x.db"), "C:/")
            self.assertIsNone(nova_database._sqlite_parent_dir("C:/x.db"))
            self.assertIsNone(nova_database._sqlite_parent_dir("C:\\x.db"))
        else:
            self.assertIsNone(nova_database._sqlite_parent_dir("/x.db"))
        self.assertIsNone(nova_database._sqlite_parent_dir("relative.db"))
        self.assertEqual(nova_database._sqlite_parent_dir(os.path.join("a", "b")), "a")

    def test_flat_file_in_an_existing_dir_only_prepares_the_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "flat.db"
            with self._with_url(f"sqlite:///{target.as_posix()}"):
                self.assertTrue(ensure_sqlite_dir())
            self.assertTrue(target.parent.is_dir())
            self.assertFalse(target.exists())

    def test_in_memory_and_other_backends_are_noops(self):
        import logging

        for url in ("sqlite://", "sqlite:///:memory:", "postgresql://u:p@h/db"):
            with self.subTest(url=url):
                with self._with_url(url), self.assertNoLogs("nova.database", logging.WARNING):
                    self.assertFalse(ensure_sqlite_dir())


if __name__ == "__main__":
    unittest.main()
