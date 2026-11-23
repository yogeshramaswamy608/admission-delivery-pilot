"""Offline checks for the H3 launcher and evidence archive."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import run_h3


class RunnerTest(unittest.TestCase):
    def test_exception_retains_evidence_and_runs_other_cases(self):
        calls = []
        def case(mode, folder, *_):
            calls.append(mode)
            (folder / mode).mkdir()
            if mode == "replay":
                (folder / mode / "raw.response").write_bytes(b"partial")
                raise TimeoutError("snapshot timed out")
            return {"result": "PASS"}

        with tempfile.TemporaryDirectory() as temp, patch.object(run_h3.compare, "run_case", side_effect=case):
            root = Path(temp)
            summary = {"cases": {}}
            run_h3.run_cases(root, {}, [], None, summary)
            self.assertEqual(calls, ["off", "replay", "search"])
            self.assertEqual(summary["status"], "UNVERIFIED")
            self.assertEqual((root / "replay/raw.response").read_bytes(), b"partial")
            self.assertIn("TimeoutError", json.loads((root / "replay/score.json").read_bytes())["reason"])

    def test_archive_preserves_nested_binary_and_empty_response(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "unique-attempt"
            (root / "off").mkdir(parents=True)
            (root / "off/notice.response").write_bytes(bytes(range(256)))
            (root / "off/empty.response").touch()
            path, count = run_h3.archive_evidence(root)
            self.assertEqual(count, 3)
            with ZipFile(path) as archive:
                self.assertEqual(archive.read("off/notice.response"), bytes(range(256)))
                self.assertEqual(archive.read("off/empty.response"), b"")

    def test_archive_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "attempt"
            root.mkdir()
            (root / "outside").symlink_to(Path(temp) / "elsewhere")
            with self.assertRaisesRegex(ValueError, "Unexpected evidence file type"):
                run_h3.archive_evidence(root)


if __name__ == "__main__":
    unittest.main()
