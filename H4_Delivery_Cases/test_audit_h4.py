"""Negative controls for the independent H4 raw evidence audit."""
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

import audit_h4 as audit


REPLAY = audit.ROOT.parent / "samples"
ROW = audit.read(REPLAY / "receiver-row.json")[0]
SOURCE = audit.read(REPLAY / "source-encounter.json")
FIXTURES = {k: (audit.KIT / "fixtures" / f"admission-{n}.json").read_bytes()
            for n, k in enumerate(audit.ADMISSIONS, 1)}


class AuditControls(unittest.TestCase):
    def test_archive_empty_response_and_post_archive_modification(self):
        with TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            run.mkdir()
            (run / "empty.response").write_bytes(b"")
            (run / "SHA256SUMS").write_text(sha256(b"").hexdigest() + "  empty.response\n")
            with ZipFile(run.parent / "H4-run.zip", "w") as archive:
                for member in run.iterdir():
                    archive.write(member, member.name)
            self.assertEqual(audit.verify_archive(run)["entries"], 2)
            (run / "empty.response").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "checksum"):
                audit.verify_archive(run)

    def test_duplicate_requires_specific_key_and_constraint(self):
        msg = (REPLAY / "duplicate-response.json").read_bytes()
        self.assertTrue(audit.duplicate(msg))
        for original in (b"uc_notification_context_v2", b"drill-adm-3", b"drill-patient-1"):
            self.assertFalse(audit.duplicate(msg.replace(original, b"wrong")))

    def test_complete_patient_snapshot_counts_and_provenance(self):
        rows, ids = [], {}
        for admission, raw in FIXTURES.items():
            ids[admission] = "reference-" + admission
            rows.append(dict(ROW, id="row-"+admission, notificationResourceId=admission,
                             notificationData=raw.decode(), xRequestId=ids[admission]))
        def inspect(data):
            ev = audit.Evidence(REPLAY)
            ev.observations["deadline_raw"] = "test.response"
            receipt = {"status":200,"raw":REPLAY/"test.response","body":json.dumps(data).encode()}
            audit.audit_records(ev, "duplicate", "off", "deadline", [receipt], ids, FIXTURES, SOURCE)
            return ev
        self.assertFalse(inspect(list(reversed(rows))).fail)
        self.assertTrue(inspect(rows[:-1]).fail)
        self.assertTrue(inspect(rows + [rows[-1]]).fail)
        self.assertTrue(inspect(rows[:-1] + [dict(rows[-1], xRequestId="swapped")]).fail)


if __name__ == "__main__":
    unittest.main()
