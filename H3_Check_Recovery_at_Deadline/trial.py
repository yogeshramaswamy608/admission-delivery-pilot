"""Capture the fixed H3 deadline independently of the recovery worker."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import sys
from threading import Event, Lock
import time
from uuid import uuid4
from xml.etree.ElementTree import ParseError

H2 = Path(__file__).resolve().parent.parent / "H2_Compare_Recovery"
sys.path.insert(0, str(H2))
from compare import DELIVERED, Exchanges, KIT, require, save, utc

GRACE = 1
DEADLINE = 5
LATE = 6
FIXTURE_HASH = "28e5c8f0cd5a09647aa465fab5575d24f8e4b34b78d9738126b4da04691726ca"


def write_order(started, ended, cutoff):
    if started is None and ended is None:
        return "none"
    if started is not None and ended is not None and started <= ended < cutoff:
        return "before"
    if started is not None and started > cutoff and (ended is None or ended >= started):
        return "after"
    return "overlap"


def score_snapshot(status, body, expected_count, expected_encounter, checker, expected_request_id=None):
    score = dict(result="UNVERIFIED", rows=None, row_id=None, request_id=None, reason="")
    if status == 204 and not body:
        records = []
    elif status == 200:
        try:
            records = json.loads(body)
            if not isinstance(records, list):
                raise ValueError("Expected an array of receiver records.")
        except (UnicodeError, ValueError) as error:
            return dict(score, reason=str(error))
    else:
        return dict(score, reason=f"Unreadable receiver snapshot, HTTP {status}.")
    score["rows"] = len(records)
    if len(records) != expected_count:
        return dict(score, result="FAIL", reason=f"Expected {expected_count} row(s), read {len(records)}.")
    if expected_count:
        if expected_encounter is None:
            return dict(score, reason="The independent reference Encounter was unavailable.")
        try:
            checker.check_row(body, expected_encounter)
            row = records[0]
            score.update(row_id=row["id"], request_id=row["xRequestId"])
            require(score["row_id"] is not None and bool(score["request_id"]), "Missing stored row or request ID.")
            if expected_request_id is not None:
                require(score["request_id"] == expected_request_id, "Stored request ID differs from the original delivery.")
        except (ValueError, TypeError, AttributeError, KeyError, IndexError, ParseError) as error:
            if str(error) == "Complete stored Encounter differs.":
                bundle = json.loads(records[0]["notificationData"])
                encounters = [entry["resource"] for entry in bundle["entry"]
                              if entry.get("resource", {}).get("resourceType") == "Encounter"]
                if len(encounters) == 1:
                    actual = checker.comparable(encounters[0])
                    expected = checker.comparable(expected_encounter)
                    score["differing_fields"] = sorted(key for key in actual.keys() | expected.keys()
                                                       if key not in actual or key not in expected or actual[key] != expected[key])
            return dict(score, result="FAIL", reason=str(error))
    return dict(score, result="PASS")


def fixture(number):
    return (KIT / "fixtures" / f"admission-{number}.json").read_bytes()


class Clock:
    def __init__(self, folder, zero):
        self.folder = folder
        self.zero = zero
        self.cutoff = zero + DEADLINE
        self.snapshot_saved = Event()
        self.lock = Lock()
        self.write_started = self.write_ended = None
        self.events = []
        self.log("notice_4_acknowledged", 0, monotonic=zero,
                 cutoff_monotonic=self.cutoff, late_monotonic=zero + LATE)

    def log(self, name, tick, **values):
        with self.lock:
            now = values.pop("monotonic", time.monotonic())
            entry = dict(event=name, tick=tick, monotonic=now, elapsed=now - self.zero, utc=utc(), **values)
            self.events.append(entry)
            with (self.folder / "clock.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps(entry) + "\n")

    def wait(self, tick):
        time.sleep(max(0, self.zero + tick - time.monotonic()))

    def recovery_post(self, http, payload, request_id):
        # The only recovery writer. New writes reaching the cutoff wait for the saved snapshot.
        with self.lock:
            now = time.monotonic()
            before = now < self.cutoff
            if before:
                self.write_started = now
        if not before:
            self.snapshot_saved.wait()
            with self.lock:
                self.write_started = time.monotonic()
        self.log("recovery_post_begin", None, request_id=str(request_id), monotonic=self.write_started)
        answer = http.post(payload, request_id)
        with self.lock:
            self.write_ended = time.monotonic()
        self.log("recovery_post_ack", None, status=answer[0], monotonic=self.write_ended)
        return answer


def capture(http, clock, tick, label, target, folder):
    clock.log(label + "_begin", tick)
    try:
        status, body = http.receiver("GET", target)
        entry = http.folder / f"{http.counter:02}-receiver.response"
        require(entry.read_bytes() == body, "Saved snapshot differs from receiver bytes.")
        proof = dict(status=status, raw=str(entry.relative_to(folder)), sha256=sha256(body).hexdigest())
    except Exception as error:
        status, body = None, b""
        proof = dict(status=None, error=str(error))
    clock.log(label + "_saved", tick, **proof)
    return status, body


def check_reference(http, base, original, checker):
    code, raw = http.source(base.rstrip("/") + "/Encounter/drill-adm-3", kind="score-source")
    require(code == 200, "Cannot retrieve the scoring reference.")
    source = json.loads(raw)
    meta = dict(source["meta"])
    require(bool(meta.pop("versionId")) and bool(meta.pop("lastUpdated")), "Missing source metadata.")
    clinical = dict(source)
    if meta:
        clinical["meta"] = meta
    else:
        clinical.pop("meta")
    require(checker.comparable(clinical) == checker.comparable(original), "Source differs from the approved original.")
    return source


def exercise(mode, folder, gate, docker, build, checker, layer_type):
    base = gate["base_url"]
    require(gate["patient_id"] == "drill-patient-1", "Unexpected source patient.")
    def channel(name):
        directory = folder / name
        directory.mkdir()
        return Exchanges(directory, base, docker, build["receiver"]["container_id"])
    main = channel("receiver")
    worker = channel("recovery")
    scoring = channel("scoring")
    target = checker.lookup("drill-adm-3")
    for number in range(1, 5):
        require(main.receiver("GET", checker.lookup(f"drill-adm-{number}")) == (204, b""), "Receiver not initially empty.")

    originals = {}
    for number in DELIVERED:
        payload, request_id = fixture(number), str(uuid4())
        status, _ = main.post(payload, request_id)
        ack = time.monotonic()
        require(status == 200, f"Receiver did not accept original {number}.")
        originals[number] = payload, request_id, ack
        if number != 4:
            code, body = main.receiver("GET", checker.lookup(f"drill-adm-{number}"))
            require(code == 200, f"Original {number} lookup failed.")
            checker.check_row(body, checker.expected_encounter(json.loads(payload)))
    clock = Clock(folder, originals[4][2])
    layer = layer_type(base, gate["subscription_id"], gate["patient_id"], GRACE, worker.source,
                       lambda body, rid: clock.recovery_post(worker, body, rid))
    for payload, _, ack in originals.values():
        layer.observe_forwarded(payload, ack)
    gap = layer.gap()
    require(gap and gap.event_number == 3 and gap.eligible_at == clock.zero + GRACE, "Unexpected gap or clock anchor.")

    def recover():
        status, body = worker.receiver("GET", checker.lookup("drill-adm-4"))
        require(status == 200, "No stored fourth admission.")
        checker.check_row(body, checker.expected_encounter(json.loads(originals[4][0])))
        clock.wait(GRACE)
        clock.log("grace_elapsed", GRACE)
        if mode != "off":
            result = layer.recover(mode, time.monotonic())
            require(result is not None, "No recovery attempted.")
            return result.request_id
        return None

    original_id = str(uuid4())
    errors = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(recover)
        try:
            clock.wait(DEADLINE)
            deadline_reply = capture(main, clock, DEADLINE, "deadline", target, folder)
        finally:
            clock.snapshot_saved.set()
        clock.wait(LATE)
        late = fixture(3)
        require(sha256(late).hexdigest() == FIXTURE_HASH, "The withheld original changed.")
        original = checker.expected_encounter(json.loads(late))
        clock.log("release_original", LATE, request_id=original_id, sha256=FIXTURE_HASH)
        try:
            late_status, late_body = main.post(late, original_id)
        except Exception as error:
            late_status, late_body = None, b""
            errors.append("Late delivery transport: " + str(error))
        clock.log("late_original_response", LATE, status=late_status)
        try:
            recovered_id = future.result()
        except Exception as error:
            recovered_id = None
            errors.append("Recovery or initialization: " + str(error))
        final_reply = capture(main, clock, LATE, "final", target, folder)

    recovered = None
    if mode != "off":
        try:
            recovered = check_reference(scoring, base, original, checker)
        except Exception as error:
            errors.append("Scoring reference: " + str(error))
    deadline = score_snapshot(*deadline_reply, int(mode != "off"), recovered, checker, recovered_id)
    final = score_snapshot(*final_reply, 1, original if mode == "off" else recovered, checker,
                           original_id if mode == "off" else recovered_id)
    order = write_order(clock.write_started, clock.write_ended, clock.cutoff)
    if order == "overlap":
        deadline.update(result="UNVERIFIED", reason="Recovery write ordering at the deadline could not be established.")
    if mode != "off" and deadline["result"] == final["result"] == "PASS":
        if (deadline["row_id"], deadline["request_id"]) != (final["row_id"], final["request_id"]):
            final.update(result="FAIL", reason="The late original replaced the recovered row or its recovery request ID.")
    late_response = {"http": late_status}
    if late_status == 422:
        try:
            checker.check_duplicate(late_body)
            late_response["duplicate_constraint"] = "uc_notification_context_v2" in late_body.decode().lower()
        except (ValueError, AttributeError, KeyError, TypeError) as error:
            late_response["detail"] = str(error)
    if mode == "off" and late_status != 200:
        errors.append("The original control delivery was not acknowledged with HTTP 200.")
    verdicts = {deadline["result"], final["result"]}
    verdict = "FAIL" if "FAIL" in verdicts else "UNVERIFIED" if errors or "UNVERIFIED" in verdicts else "PASS"
    return dict(result=verdict, deadline=deadline, final=final, recovery_write_order=order,
                recovery_request_id=recovered_id, late_request_id=original_id, late_response=late_response, errors=errors)
