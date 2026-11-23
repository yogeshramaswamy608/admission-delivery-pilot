"""Verify the deadline scorer and write ordering without a live receiver."""

import importlib.util
import json
from pathlib import Path
import unittest

from trial import score_snapshot, write_order


ROOT = Path(__file__).resolve().parent.parent
H1_CHECKER = ROOT / "H1_Validate_the_Receiver" / "local-wsl-v3" / "receiver_check.py"
SAMPLES = ROOT / "samples"


def encode(value):
    return json.dumps(value).encode("utf-8")


class SnapshotScoring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("h1_snapshot_checker", H1_CHECKER)
        cls.checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.checker)
        cls.raw = (SAMPLES / "receiver-row.json").read_bytes()
        cls.source = json.loads((SAMPLES / "source-encounter.json").read_bytes())
        cls.row, = json.loads(cls.raw)

    def score(self, status, body, count=1, expected_request_id=None):
        return score_snapshot(status, body, count, self.source, self.checker, expected_request_id)

    def with_encounter_change(self, change):
        row, = json.loads(self.raw)
        envelope = json.loads(row["notificationData"])
        encounter, = [entry["resource"] for entry in envelope["entry"]
                      if entry["resource"]["resourceType"] == "Encounter"]
        change(encounter)
        row["notificationData"] = json.dumps(envelope)
        return encode([row])

    def test_unreadable_http_or_json_is_unverified(self):
        for status, body in [(503, b"Unavailable"), (200, b"{"), (200, b""),
                             (200, b'{"resourceType":"OperationOutcome"}'), (204, b"not empty")]:
            with self.subTest(status=status, body=body):
                result = self.score(status, body)
                self.assertEqual(result["result"], "UNVERIFIED")
                self.assertIsNone(result["rows"])

    def test_empty_lookup_passes_only_when_zero_rows_expected(self):
        for status, body in [(204, b""), (200, b"[]")]:
            with self.subTest(status=status):
                result = self.score(status, body, count=0)
                self.assertEqual((result["result"], result["rows"], result["row_id"]), ("PASS", 0, None))
                result = self.score(status, body)
                self.assertEqual((result["result"], result["rows"]), ("FAIL", 0))

    def test_counts_all_rows_before_evaluating_content(self):
        other = {**self.row, "id": "second-row", "triggerEvent": "wrong-trigger"}
        result = self.score(200, encode([self.row, other]), expected_request_id=self.row["xRequestId"])
        self.assertEqual((result["result"], result["rows"]), ("FAIL", 2))

    def test_readable_incomplete_or_wrong_encounter_fails(self):
        changes = {
            "missing period": lambda encounter: encounter.pop("period"),
            "wrong admission identifier": lambda encounter: encounter["identifier"][0].update(value="OTHER-ADMISSION"),
            "lost version metadata": lambda encounter: encounter.pop("meta"),
        }
        for label, change in changes.items():
            with self.subTest(label=label):
                result = self.score(200, self.with_encounter_change(change))
                self.assertEqual((result["result"], result["rows"]), ("FAIL", 1))

    def test_exact_recovered_row_retains_source_fields_and_request_id(self):
        result = self.score(200, self.raw, expected_request_id=self.row["xRequestId"])
        self.assertEqual((result["result"], result["rows"]), ("PASS", 1))
        self.assertEqual(result["row_id"], self.row["id"])
        self.assertEqual(result["request_id"], self.row["xRequestId"])

    def test_matching_encounter_with_wrong_recovery_request_id_fails(self):
        result = self.score(200, self.raw, expected_request_id="a-different-recovery-request")
        self.assertEqual((result["result"], result["rows"]), ("FAIL", 1))


class WriteOrdering(unittest.TestCase):
    def test_classifies_receipts_on_either_side_of_fixed_boundary(self):
        cutoff = 5_000_000_000
        for started, ended, expected in [
            (None, None, "none"),
            (cutoff - 2, cutoff - 1, "before"),
            (cutoff + 1, cutoff + 2, "after"),
            (cutoff - 1, cutoff + 1, "overlap"),
            (cutoff - 1, cutoff, "overlap"),
            (cutoff, cutoff + 1, "overlap"),
            (cutoff - 1, None, "overlap"),
        ]:
            with self.subTest(started=started, ended=ended):
                self.assertEqual(write_order(started, ended, cutoff), expected)
