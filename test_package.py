"""Exercise the package inventory without starting a server or accessing Docker."""
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import run


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.work = TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        contents = {"run.py": b"sample", "README.md": b"guide", "DECISIONS.md": b"decisions",
                    "VERSION": (run.VERSION + "\n").encode(), "fixtures/example": b"original"}
        for name, data in contents.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.manifest = self.root / "SHA256SUMS"
        self.manifest.write_text("".join(f"{sha256(data).hexdigest()}  {name}\n" for name, data in contents.items()))

    def test_intact_and_generated_evidence(self):
        self.assertEqual(run.verify(self.root), 5)
        output = self.root / "run-history"
        output.mkdir()
        (output / "launch.json").write_text("{}")
        self.assertEqual(run.verify(self.root), 5)

    def test_changed_missing_and_unlisted_input(self):
        fixture = self.root / "fixtures/example"
        fixture.write_bytes(b"other")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            run.verify(self.root)
        fixture.unlink()
        with self.assertRaisesRegex(ValueError, "Missing distribution files"):
            run.verify(self.root)
        fixture.write_bytes(b"original")
        (self.root / "unexpected.py").touch()
        with self.assertRaisesRegex(ValueError, "Unexpected distribution file"):
            run.verify(self.root)

    def test_duplicate_entry_unsafe_path_and_unexpected_link(self):
        valid = self.manifest.read_text()
        first = valid.splitlines()[0]
        for entry in (first, first.split("  ")[0] + "  ../outside",
                      first.split("  ")[0] + "  /outside", first.split("  ")[0] + "  run-history/file"):
            with self.subTest(entry=entry):
                self.manifest.write_text(valid + entry + "\n")
                with self.assertRaisesRegex(ValueError, "Invalid distribution manifest"):
                    run.verify(self.root)
        self.manifest.write_text(valid)
        (self.root / "run-history").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Unexpected link"):
            run.verify(self.root)

    def test_empty_invalid_and_version_mismatch(self):
        valid = self.manifest.read_text()
        self.manifest.write_text("")
        with self.assertRaises(ValueError):
            run.verify(self.root)
        self.manifest.write_text("not a checksum line\n")
        with self.assertRaises(ValueError):
            run.verify(self.root)
        data = b"v999999999\n"
        (self.root / "VERSION").write_bytes(data)
        lines = [line for line in valid.splitlines() if not line.endswith("  VERSION")]
        self.manifest.write_text("\n".join(lines + [f"{sha256(data).hexdigest()}  VERSION"]) + "\n")
        with self.assertRaisesRegex(ValueError, "distribution identity"):
            run.verify(self.root)
