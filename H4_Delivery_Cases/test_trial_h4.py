"""Exercise H4's matrix and failure classifications without contacting a receiver."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

import trial_h4 as h4
from compare import KIT
from recovery import RecoveryLayer
sys.path.insert(0, str(KIT))
import receiver_check as checker

SAMPLES = h4.PROJECT / "samples"
GATE = json.loads((SAMPLES / "test-context.json").read_bytes())
THIRD = "drill-adm-3"
SOURCE = json.loads((SAMPLES / "source-encounter.json").read_bytes())
ROW = json.loads((SAMPLES / "receiver-row.json").read_bytes())[0]
DUP = (SAMPLES / "duplicate-response.json").read_bytes()


class ScoringTests(unittest.TestCase):
    def score(self, status, rows):
        return h4.judge_patient(status, rows, {THIRD: SOURCE}, {THIRD: ROW["xRequestId"]}, {THIRD}, checker)

    def test_score_records_in_any_order_and_trace_ids(self):
        self.assertEqual(self.score(200, json.dumps([ROW]).encode())["result"], "PASS")

    def test_duplicate_extra_and_incomplete_records_fail(self):
        for change in ("duplicate", "extra", "incomplete", "wrong_request"):
            rows = [deepcopy(ROW)]
            if change == "duplicate":
                rows += [ROW]
            elif change == "extra":
                rows.append(dict(ROW, notificationResourceId="unexpected-admission"))
            elif change == "incomplete":
                notice = json.loads(rows[0]["notificationData"])
                notice["entry"][1]["resource"].pop("period")
                rows[0]["notificationData"] = json.dumps(notice)
            else:
                rows[0]["xRequestId"] = "unrelated-request"
            with self.subTest(change=change):
                self.assertEqual(self.score(200, json.dumps(rows).encode())["result"], "FAIL")

    def test_empty_and_invalid_reads_distinguish_absence_from_uncertainty(self):
        self.assertEqual(self.score(204, b"")["result"], "FAIL")
        for status, payload in [(204, b"nonempty"), (200, b"{}"), (200, b""), (503, b"")]:
            self.assertEqual(self.score(status, payload)["result"], "UNVERIFIED")

    def test_duplicate_matches_only_known_key_and_exact_constraint(self):
        self.assertTrue(h4.duplicate_matches(422, DUP))
        for status, body in [(200, DUP), (422, b"{}"), (422, b"invalid"),
                             (422, DUP.replace(b"drill-patient-1", b"unrelated-patient")),
                             (422, DUP.replace(b"uc_notification_context_v2", b"other_constraint"))]:
            self.assertFalse(h4.duplicate_matches(status, body))

    def test_all_fifteen_expected_pairs(self):
        for scenario in h4.SCENARIOS:
            for mode in h4.MODES:
                pair = [int(THIRD in h4.expected_placements(scenario, mode, final)) for final in (False, True)]
                expected = [1, 1]
                if scenario == "missing" and mode == "off":
                    expected = [0, 0]
                if scenario == "late" and mode == "off":
                    expected = [0, 1]
                self.assertEqual(pair, expected)


class MemoryChannel:
    def __init__(self, folder, storage):
        self.folder = folder
        folder.mkdir()
        self.storage = storage
        self.counter = 0

    def post(self, body, rid):
        envelope = json.loads(body)
        adm = checker.expected_encounter(envelope)["id"]
        if adm in self.storage:
            return 422, DUP
        self.storage[adm] = dict(ROW, id=f"row-{adm}", xRequestId=str(rid), notificationData=body.decode(),
                                 notificationResourceId=adm)
        return 200, b""

    def receiver(self, method, path):
        self.counter += 1
        if path == h4.PATIENT_LOOKUP:
            rows = list(reversed(self.storage.values()))
        else:
            rows = [v for k, v in self.storage.items() if path == checker.lookup(k)]
        body = json.dumps(rows).encode() if rows else b""
        (self.folder / f"{self.counter:02}-receiver.response").write_bytes(body)
        return (200 if rows else 204), body

    def source(self, url, kind="source"):
        if kind == "score-source":
            return 200, json.dumps(SOURCE).encode()
        name = "replay-event-3.response" if "$events" in url else "search-patient-encounters.response"
        return 200, (SAMPLES / name).read_bytes()


class TrialMatrix(unittest.TestCase):
    def test_offline_end_to_end_matrix(self):
        def one(parent, scenario, mode):
            work = parent / (scenario + "-" + mode)
            work.mkdir()
            stored = {}
            def channel(name):
                return MemoryChannel(work / name, stored)
            score = h4.trial_body(scenario, mode, work, GATE, checker, RecoveryLayer, channel)
            return scenario, mode, score
        with TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=15) as pool:
            futures = [pool.submit(one, Path(directory), s, m) for s in h4.SCENARIOS for m in h4.MODES]
            for future in futures:
                s, m, result = future.result()
                with self.subTest(scenario=s, mode=m):
                    self.assertEqual(result["result"], "PASS", result)
                    self.assertEqual(result["recovery"]["posts"], int(m != "off" and s in ("late", "missing")))
                    self.assertEqual([result[k]["admission_three_rows"] for k in ("deadline", "final")],
                                     [int(THIRD in h4.expected_placements(s, m, phase)) for phase in (False, True)])


if __name__ == "__main__":
    unittest.main()
