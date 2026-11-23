"""Run one H4 delivery schedule on an already provisioned, isolated receiver."""
import base64
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys
from threading import Event, Lock
import time
from uuid import uuid4

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "H3_Check_Recovery_at_Deadline"))
from trial import Clock, DEADLINE, FIXTURE_HASH, GRACE, LATE, capture, check_reference, fixture, score_snapshot, write_order
from compare import CONTAINER_CLIENT, Exchanges, TESTER, require, save, utc

SCENARIOS = ("normal", "missing", "late", "reordered", "duplicate")
MODES = ("off", "replay", "search")
PATIENT = "drill-patient-1"
PATIENT_LOOKUP = "/api/notificationContexts?patientId=" + PATIENT
DUPLICATE_KEY = ("Key (fhir_server_base_url, patient_id, notification_resource_id, notification_resource_type, "
                 "trigger_event)=(https://example.org/fhir, drill-patient-1, drill-adm-3, Encounter, encounter-start) already exists.")


@contextmanager
def test_client(folder, docker, receiver_id):
    """Retain one pinned private tester. Each docker exec makes a separate, non-retrying HTTP connection."""
    command = docker + ["run", "--detach", "--rm", "--pull=never", "--platform", "linux/amd64",
                        "--network", "container:" + receiver_id, "--cap-drop", "ALL", "--read-only",
                        "--security-opt", "no-new-privileges", TESTER, "python", "-I", "-c",
                        "import signal,time,sys; signal.signal(signal.SIGTERM,lambda *_: sys.exit(0)); time.sleep(600)"]
    container = subprocess.run(command, check=True, capture_output=True, text=True, timeout=45).stdout.strip()
    require(re.fullmatch(r"[0-9a-f]{64}", container), "Unexpected private test-client container ID.")
    receipt = dict(container_id=container, receiver_network_namespace=receiver_id, image=TESTER, started=utc())
    try:
        save(folder / "test-client.json", receipt)
        yield container
    finally:
        stopped = subprocess.run(docker + ["stop", "--time", "2", container], capture_output=True, timeout=20)
        receipt.update(stop_exit=stopped.returncode, stopped=utc())
        save(folder / "test-client.json", receipt)
        require(stopped.returncode == 0, "Private HTTP test client did not stop cleanly.")


class TrialExchanges(Exchanges):
    def receiver(self, method, path, body=None, request_id=None):
        headers = {"Accept": "application/json"}
        if body is not None:
            headers.update({"X-Request-ID": request_id, "Content-Type": "application/fhir+json"})
        stem, meta = self.begin("receiver", method, path, headers, body)
        request = dict(method=method, path=path, headers=headers,
                       body=None if body is None else base64.b64encode(body).decode())
        cmd = self.docker + ["exec", "-i", self.container_id, "python", "-I", "-c", CONTAINER_CLIENT]
        process = subprocess.run(cmd, input=json.dumps(request).encode(), capture_output=True, timeout=35)
        require(process.returncode == 0, f"Receiver test client exited {process.returncode} at {stem.name}.")
        status, reply_headers, encoded = json.loads(process.stdout)
        return self.finish(stem, meta, status, reply_headers, base64.b64decode(encoded))


def duplicate_matches(status, body):
    if status != 422:
        return False
    try:
        outcome = json.loads(body)
        return outcome["resourceType"] == "OperationOutcome" and any(
            issue.get("severity") == "error" and issue.get("code") == "invalid"
            and 'unique constraint "uc_notification_context_v2"' in issue["details"]["text"]
            and DUPLICATE_KEY in issue["details"]["text"] for issue in outcome["issue"])
    except (TypeError, AttributeError, KeyError, UnicodeError, ValueError):
        return False


def judge_patient(status, body, references, request_ids, expected_ids, checker):
    score = dict(result="UNVERIFIED", total_rows=None, admission_three_rows=None, records={}, reasons=[])
    if status == 204 and not body:
        rows = []
    elif status == 200:
        try:
            rows = json.loads(body)
            require(isinstance(rows, list), "Expected an array from the patient lookup.")
        except (ValueError, UnicodeError) as error:
            score["reasons"].append(str(error))
            return score
    else:
        score["reasons"].append(f"Unreadable patient lookup, HTTP {status}.")
        return score
    score["total_rows"] = len(rows)
    by_admission = defaultdict(list)
    for row in rows:
        admission = row.get("notificationResourceId") if isinstance(row, dict) else None
        by_admission[admission if isinstance(admission, str) else "<missing admission ID>"].append(row)
    score["admission_three_rows"] = len(by_admission.get("drill-adm-3", []))
    outcomes = set()
    for adm in sorted(set(by_admission) | expected_ids | {"drill-adm-3"}):
        entries = by_admission.get(adm, [])
        item = score_snapshot(200, json.dumps(entries).encode(), int(adm in expected_ids),
                              references.get(adm), checker, request_ids.get(adm))
        if entries and adm in expected_ids and item["result"] == "PASS":
            if entries[0].get("fhirServerBaseUrl") != "https://example.org/fhir":
                item.update(result="FAIL", reason="Unexpected original notification's FHIR server base URL.")
            elif not request_ids.get(adm):
                item.update(result="UNVERIFIED", reason="No acknowledged delivery request ID to compare.")
        score["records"][adm] = item
        outcomes.add(item["result"])
        if item["reason"]:
            score["reasons"].append(f"{adm}: {item['reason']}")
    score["result"] = "FAIL" if "FAIL" in outcomes else "UNVERIFIED" if "UNVERIFIED" in outcomes else "PASS"
    return score


def expected_placements(scenario, mode, final):
    ids = {"drill-adm-1", "drill-adm-2", "drill-adm-4"}
    if mode != "off" or scenario not in ("missing", "late") or (final and scenario == "late"):
        ids.add("drill-adm-3")
    return ids


def trial_body(scenario, mode, folder, gate, checker, layer_type, channel):
    main, delivery, worker, scoring = (channel(s) for s in ("receiver", "delivery", "recovery", "scoring"))
    reasons, uncertain = [], []
    original_ids, references, originals = {}, {}, {}
    prior_duplicate_id = None
    for n in range(1, 5):
        status, body = main.receiver("GET", checker.lookup(f"drill-adm-{n}"))
        if status != 204 or body:
            score = "FAIL" if status in (200, 204) else "UNVERIFIED"
            return dict(result=score, deadline=None, final=None, reasons=[f"Initial admission {n} lookup returned {status}."])
    initial = (1, 2, 3, 4) if scenario in ("normal", "duplicate") else (1, 2, 4)
    for n in initial:
        payload, rid = fixture(n), str(uuid4())
        if n == 3:
            require(sha256(payload).hexdigest() == FIXTURE_HASH, "Original fixture 3 changed.")
        status, _ = main.post(payload, rid)
        ack = time.monotonic()
        if status != 200:
            return dict(result="FAIL", deadline=None, final=None, reasons=[f"Original {n} returned HTTP {status}."])
        aid = f"drill-adm-{n}"
        references[aid] = checker.expected_encounter(json.loads(payload))
        original_ids[aid] = rid
        originals[n] = (payload, ack)
        if n == 3 and scenario == "duplicate":
            code, data = main.receiver("GET", checker.lookup(aid))
            prior = score_snapshot(code, data, 1, references[aid], checker, rid)
            save(folder / "before-repeat.json", prior)
            if prior["result"] != "PASS":
                return dict(result=prior["result"], deadline=None, final=None, reasons=[prior["reason"]])
            prior_duplicate_id = prior["row_id"]

    clock = Clock(folder, originals[4][1])
    calls = dict(source=0, posts=0)
    written = {}
    writes_lock = Lock()
    final_saved = Event()
    layer_lock = Lock()
    def source(url):
        calls["source"] += 1
        return worker.source(url)

    def post_in_window(http, payload, rid, label, tick):
        # A worker arriving after the cutoff may write only after the raw deadline snapshot has been saved.
        start = time.monotonic()
        if start >= clock.cutoff:
            clock.snapshot_saved.wait()
            start = time.monotonic()
        if start >= clock.zero + LATE and tick != LATE:
            final_saved.wait()
            start = time.monotonic()
        with writes_lock:
            attempt = dict(request_id=rid, started=start, ended=None, status=None)
            written[label] = attempt
        clock.log(label + "_begin", tick, monotonic=start, request_id=rid, sha256=sha256(payload).hexdigest())
        status, body = http.post(payload, rid)
        ack = time.monotonic()
        with writes_lock:
            attempt.update(ended=ack, status=status)
        clock.log(label + "_ack", tick, monotonic=ack, request_id=rid, status=status)
        return status, body

    def recovery_post(payload, rid):
        calls["posts"] += 1
        return post_in_window(worker, payload, str(rid), "recovery_post", None)

    layer = layer_type(gate["base_url"], gate["subscription_id"], PATIENT, GRACE, source, recovery_post)
    for payload, ack in originals.values():
        layer.observe_forwarded(payload, ack)
    def recovery():
        clock.wait(GRACE)
        clock.log("grace_elapsed", GRACE)
        if mode != "off":
            with layer_lock:
                return layer.recover(mode, time.monotonic())
        return None

    def scheduled(tick):
        clock.wait(tick)
        payload, rid = fixture(3), str(uuid4())
        require(sha256(payload).hexdigest() == FIXTURE_HASH, "Original fixture 3 changed.")
        status, body = post_in_window(delivery, payload, rid, "scheduled_post", tick)
        ack = written["scheduled_post"]["ended"]
        if scenario == "reordered" and status == 200:
            with layer_lock:
                layer.observe_forwarded(payload, ack)
        return status, body, rid

    scheduled_reply = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        schedule_job = None
        if scenario in ("duplicate", "reordered"):
            schedule_job = pool.submit(scheduled, 2 if scenario == "duplicate" else 0.25)
        recovery_job = pool.submit(recovery)
        try:
            clock.wait(DEADLINE)
            deadline_reply = capture(main, clock, DEADLINE, "deadline", PATIENT_LOOKUP, folder)
        finally:
            clock.snapshot_saved.set()
        clock.wait(LATE)
        if scenario == "late":
            schedule_job = pool.submit(scheduled, LATE)
        if schedule_job and scenario == "late":
            try:
                scheduled_reply = schedule_job.result()
            except Exception as error:
                uncertain.append("Scheduled original transport or observation: " + str(error))
        try:
            final_reply = capture(main, clock, LATE, "final", PATIENT_LOOKUP, folder)
        finally:
            final_saved.set()
        if schedule_job and scenario != "late":
            try:
                scheduled_reply = schedule_job.result()
            except Exception as error:
                uncertain.append("Scheduled original transport or observation: " + str(error))
        try:
            recovered = recovery_job.result()
        except Exception as error:
            recovered = None
            uncertain.append("Recovery could not complete: " + str(error))

    should_recover = mode != "off" and scenario in ("missing", "late")
    if calls["posts"] != int(should_recover) or bool(calls["source"]) != should_recover:
        reasons.append(f"Unexpected recovery traffic: source reads {calls['source']}, POSTs {calls['posts']}.")
    recovered_id = recovered.request_id if recovered else None
    if should_recover and calls["posts"] and written.get("recovery_post", {}).get("status") not in (None, 200):
        reasons.append(f"Recovery delivery returned HTTP {written['recovery_post']['status']}.")
    originals_three = checker.expected_encounter(json.loads(fixture(3)))
    if should_recover:
        try:
            references["drill-adm-3"] = check_reference(scoring, gate["base_url"], originals_three, checker)
        except Exception as error:
            uncertain.append("Independent source reference: " + str(error))
        original_ids["drill-adm-3"] = recovered_id
    elif scenario in ("late", "reordered"):
        references["drill-adm-3"] = originals_three
        if scheduled_reply:
            original_ids["drill-adm-3"] = scheduled_reply[2]
    is_repeat = scenario == "duplicate" or (scenario == "late" and should_recover)
    duplicate_ok = False
    if scheduled_reply:
        status, body, _ = scheduled_reply
        if is_repeat:
            duplicate_ok = duplicate_matches(status, body)
            if not duplicate_ok:
                reasons.append(f"Expected the target admission duplicate constraint and key; HTTP {status}.")
        elif status != 200:
            reasons.append(f"Expected original admission 3 to be accepted; HTTP {status}.")

    def read_score(reply, at_final):
        scored = judge_patient(*reply, references, original_ids, expected_placements(scenario, mode, at_final), checker)
        saved = next(event for event in clock.events if event["event"] == ("final_saved" if at_final else "deadline_saved"))
        scored.update(raw=saved.get("raw"), sha256=saved.get("sha256"))
        return scored
    deadline, final = read_score(deadline_reply, False), read_score(final_reply, True)
    for adm in expected_placements(scenario, mode, False):
        a, b = (s["records"].get(adm, {}) for s in (deadline, final))
        if a.get("result") == b.get("result") == "PASS" and a["row_id"] != b["row_id"]:
            reasons.append(f"{adm}: final row ID differs from the deadline row ID.")
    if prior_duplicate_id:
        for snap in (deadline, final):
            adm = snap["records"].get("drill-adm-3", {})
            if adm.get("result") == "PASS" and adm["row_id"] != prior_duplicate_id:
                reasons.append("The intentional duplicate replaced the first stored row.")

    bad_timing = []
    orders = {label: write_order(value["started"], value["ended"], clock.cutoff) for label, value in written.items()}
    if "overlap" in orders.values():
        bad_timing.append("A receiver write spanned the cutoff or lacked an ordered acknowledgment.")
    if scenario in ("reordered", "duplicate") and orders.get("scheduled_post") != "before":
        bad_timing.append("The scheduled delivery was not established before the deadline.")
    reordered_ack = written.get("scheduled_post", {}).get("ended")
    if scenario == "reordered" and (reordered_ack is None or reordered_ack >= clock.zero + GRACE):
        bad_timing.append("The reordered acknowledgment was not observed inside the one-second grace interval.")
    if bad_timing:
        deadline.update(result="UNVERIFIED")
        deadline["reasons"].extend(bad_timing)
    final_begin = next(event["monotonic"] for event in clock.events if event["event"] == "final_begin")
    final_end = next(event["monotonic"] for event in clock.events if event["event"] == "final_saved")
    for label, value in written.items():
        if value["started"] <= final_end and (value["ended"] is None or value["ended"] >= final_begin):
            final.update(result="UNVERIFIED")
            final["reasons"].append(f"{label} coincided with final snapshot capture.")
    verdicts = (deadline["result"], final["result"])
    if bad_timing:
        verdict = "UNVERIFIED"
    elif reasons or "FAIL" in verdicts:
        verdict = "FAIL"
    elif uncertain or "UNVERIFIED" in verdicts:
        verdict = "UNVERIFIED"
    else:
        verdict = "PASS"
    return dict(scenario=scenario, mode=mode, result=verdict, deadline=deadline, final=final, recovery=calls,
                recovery_request_id=recovered_id, scheduled_http=scheduled_reply[0] if scheduled_reply else None,
                duplicate_confirmed=duplicate_ok, writes=written, write_order=orders, reasons=reasons,
                unverified=uncertain+bad_timing)


def exercise(scenario, mode, folder, gate, docker, build, checker, layer_type):
    require(scenario in SCENARIOS and mode in MODES and gate["patient_id"] == PATIENT, "Invalid trial inputs.")
    with test_client(folder, docker, build["receiver"]["container_id"]) as client:
        def channel(name):
            path = folder / name
            path.mkdir()
            return TrialExchanges(path, gate["base_url"], docker, client)
        return trial_body(scenario, mode, folder, gate, checker, layer_type, channel)
