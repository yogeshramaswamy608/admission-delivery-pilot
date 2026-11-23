"""Independently read the H4 evidence for every prespecified trial and verify its ZIP."""
import argparse
from collections import Counter
from hashlib import file_digest, sha256
from itertools import product
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from xml.etree.ElementTree import ParseError, canonicalize
from zipfile import BadZipFile, ZipFile


ROOT = Path(__file__).resolve().parent
KIT = ROOT.parent / "H1_Validate_the_Receiver" / "local-wsl-v3"
CASES = ("normal", "missing", "late", "reordered", "duplicate")
MODES = ("off", "replay", "search")
PATIENT = "drill-patient-1"
THREE = "drill-adm-3"
ADMISSIONS = tuple(f"drill-adm-{n}" for n in range(1, 5))
BASE = "https://example.org/fhir"
KEY = "(https://example.org/fhir, drill-patient-1, drill-adm-3, Encounter, encounter-start)"
LOOKUP = "/api/notificationContexts"
RECEIVE = "/api/receive-notification"
RAW_ERRORS = (OSError, ValueError, KeyError, AttributeError, TypeError, IndexError, ParseError)


def read(path):
    return json.loads(path.read_bytes())


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def encounter(bundle):
    resources = [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Encounter"]
    if len(resources) != 1:
        raise ValueError(f"Expected exactly one Encounter, read {len(resources)}.")
    return resources[0]


def comparable(item):
    if "div" in item.get("text", {}):
        body = dict(item["text"], div=canonicalize(item["text"]["div"], rewrite_prefixes=True))
        return dict(item, text=body)
    return item


def duplicate(body):
    doc = json.loads(body)
    return (doc.get("resourceType") == "OperationOutcome" and
            any(i.get("severity") == "error" and i.get("code") == "invalid" and
                '"uc_notification_context_v2"' in i.get("details", {}).get("text", "") and
                KEY in i.get("details", {}).get("text", "")
                for i in doc.get("issue", [])))


def read_gate(run, fixture):
    folder = run / "prerequisites"
    gate = read(folder / "gate.json")
    if (gate["result"], gate["server_version"], gate["server_commit"], gate["patient_id"],
        gate["event_number"], gate["search_matches"]) != (
            "PASS", "2026.708.1348", "f04a1d4bdcf0265eb401b98eb0647a4ff373813e", PATIENT, 3, 4):
        raise ValueError("Pinned source or prerequisite result differs.")
    replay = read(folder / gate["replay_response"])
    search = read(folder / gate["search_response"])
    src = encounter(replay)
    matches = {e["resource"]["id"]: e["resource"] for e in search["entry"]}
    raw_paths = (folder / gate["replay_response"], folder / gate["search_response"])
    if any(read(p.with_suffix(".json"))["status"] != 200 for p in raw_paths):
        raise ValueError("A source prerequisite read did not return HTTP 200.")
    if len(search["entry"]) != 4 or set(matches) != set(ADMISSIONS) or src != matches[THREE]:
        raise ValueError("Prerequisite readbacks disagree on the source trial data.")
    clinical = src.copy()
    meta = clinical.pop("meta").copy()
    if not meta.pop("versionId") or not meta.pop("lastUpdated"):
        raise ValueError("Missing source version or timestamp.")
    if meta:
        clinical["meta"] = meta
    if comparable(clinical) != comparable(fixture):
        raise ValueError("Source differs from the fixture beyond its version and timestamp.")
    return gate, src


def verify_archive(run):
    archive = run.parent / f"H4-{run.name}.zip"
    files = {p.relative_to(run).as_posix(): p for p in run.rglob("*") if p.is_file()}
    manifest = {}
    for line in (run / "SHA256SUMS").read_text().splitlines():
        value, name = line.split("  ", 1)
        if name in manifest:
            raise ValueError(f"Duplicate manifest path {name}.")
        manifest[name] = value
    if (files.keys() - {"SHA256SUMS"}) != manifest.keys():
        raise ValueError("Disk inventory differs from checksum manifest.")
    with ZipFile(archive) as z:
        names = z.namelist()
        if len(set(names)) != len(names) or set(names) != files.keys():
            raise ValueError("Archive member list differs from the run directory.")
        for name in names:
            with z.open(name) as stream:
                stored = file_digest(stream, "sha256").hexdigest()
            if stored != digest(files[name]) or (name in manifest and stored != manifest[name]):
                raise ValueError(f"Evidence checksum mismatch: {name}.")
    return {"entries": len(files), "manifest_hashes": len(manifest), "sha256": digest(archive)}


class Evidence:
    def __init__(self, folder):
        self.folder = folder
        self.fail = []
        self.unknown = []
        self.ids = []
        self.observations = {}
        self.deploy_ids = {}

    def check(self, condition, reason):
        if not condition:
            self.fail.append(reason)

    def verify(self, condition, reason):
        if not condition:
            self.unknown.append(reason)

    def phase(self, label, callback, *args):
        try:
            return callback(*args)
        except RAW_ERRORS as exc:
            self.unknown.append(f"{label}: {type(exc).__name__}: {exc}")
            return None

    def exchanges(self):
        results = []
        for channel in ("receiver", "delivery", "recovery", "scoring"):
            for path in sorted((self.folder / channel).glob("[0-9]*-*.json")):
                meta = read(path)
                self.verify("status" in meta and "ended" in meta, f"Incomplete exchange {channel}/{path.name}.")
                if "ended" not in meta or "status" not in meta:
                    continue
                self.verify(meta["started"] <= meta["ended"], f"Inconsistent timestamps for {path.name}.")
                raw = path.with_suffix(".response")
                body = raw.read_bytes()
                sent = None
                if meta["method"] == "POST":
                    sent = path.with_suffix(".request").read_bytes()
                    self.verify(sha256(sent).hexdigest() == meta["request_sha256"], f"Request hash mismatch for {path}.")
                    self.ids.append(meta["headers"]["X-Request-ID"])
                results.append(dict(meta, path=path, raw=raw, channel=channel, body=body, sent=sent))
        return sorted(results, key=lambda x: x["started"])

    def deployment(self, expected_migrations):
        build = read(self.folder / "deployment/build.json")
        mig = read(self.folder / "deployment/migration.json")
        self.verify([(e["path"], e["git_blob"]) for e in mig] == expected_migrations and
                    all(e["exit"] == 0 for e in mig), "Migration evidence differs from pinned schema.")
        self.verify(build["source_revision"] == "89da654471b651dd70592f371e577fffa3c5f04e" and
                    build["network"]["internal"] is True and build["database"]["server_version"].startswith("15.20 ") and
                    build["receiver"]["image_id"] == "sha256:28a195c6138316988689adf21afcc2a239bf1f95dc9bc0b52e87fb113adc2976" and
                    build["database"]["image_id"] == "sha256:e924003122c1459b25ebe2af0f5b8becc8b50ccee1354b304e5ba7ec1e0bb42c",
                    "Deployment release, database or private network differs.")
        project = build["project"]
        self.deploy_ids = dict(project=project, network=build["network"]["id"],
                               receiver=build["receiver"]["container_id"], database=build["database"]["container_id"])
        client = read(self.folder / "test-client.json")
        self.verify(client["stop_exit"] == 0 and client["receiver_network_namespace"] == self.deploy_ids["receiver"]
                    and client["image"] == build["transport"]["tester_image"], "Private test-client lifecycle differs.")
        self.deploy_ids["tester"] = client["container_id"]
        stop = (self.folder / "stop.log").read_text()
        self.verify(all(any(f"{project}-{name}-1" in line and line.rstrip().endswith("Stopped") for line in stop.splitlines())
                        for name in ("database", "receiver")), "Missing expected resource stop evidence.")


def audit_clock(e, scenario, mode, xs):
    lines = [json.loads(line) for line in (e.folder / "clock.jsonl").read_text().splitlines()]
    events = {x["event"]: x for x in lines}
    for required in ("notice_4_acknowledged", "deadline_begin", "deadline_saved", "final_begin", "final_saved"):
        e.verify(sum(x['event'] == required for x in lines) == 1, f"Expected exactly one {required} event.")
    zero = events["notice_4_acknowledged"]
    e.verify(zero["tick"] == 0 and abs(zero["cutoff_monotonic"] - zero["monotonic"] - 5) < 1e-6,
             "Deadline not fixed at +5 from notice 4.")
    e.verify(events["grace_elapsed"]["tick"] == 1 and events["grace_elapsed"]["elapsed"] >= 1,
             "Grace boundary occurred early or lacked its tick.")
    last = [x for x in xs if x["channel"] == "receiver" and x["method"] == "POST"][-1]
    e.verify(encounter(json.loads(last["sent"]))["id"] == "drill-adm-4" and last["ended"] <= zero["utc"],
             "Logical zero does not follow the original fourth acknowledgment.")
    for label in ("deadline", "final"):
        begin, end = events[label+"_begin"], events[label+"_saved"]
        tick = 5 if label == "deadline" else 6
        e.verify(begin["tick"] == end["tick"] == tick and begin["elapsed"] >= tick and
                 begin["monotonic"] <= end["monotonic"], f"{label} logical snapshot timing is unverified.")
        matches = [x for x in xs if x["raw"].relative_to(e.folder).as_posix() == end["raw"]]
        if len(matches) != 1:
            raise ValueError(f"{label} snapshot lacks a unique raw exchange.")
        x, = matches
        e.verify(sha256(x["body"]).hexdigest() == end["sha256"] and x["status"] == end["status"],
                 f"{label} raw bytes unbound from clock evidence.")
        e.verify(begin["utc"] <= x["started"] <= x["ended"] <= end["utc"], f"{label} exchange outside recorded barrier.")
        url = urlsplit(x["url"])
        e.verify(x["method"] == "GET" and url.path == LOOKUP and parse_qs(url.query) == {"patientId":[PATIENT]},
                 f"{label} was not a full test-patient lookup.")
        e.observations[label+"_raw"] = end["raw"]
        e.observations[label+"_saved_elapsed"] = end["elapsed"]
    e.verify(events["deadline_saved"]["monotonic"] < events["final_begin"]["monotonic"],
             "Final snapshot began before the deadline evidence was saved.")
    all_patient = [x for x in xs if x["method"] == "GET" and urlsplit(x["url"]).path == LOOKUP
                   and parse_qs(urlsplit(x["url"]).query) == {"patientId": [PATIENT]}]
    e.verify(len(all_patient) == 2, f"Expected two complete patient snapshots, found {len(all_patient)}.")
    if scenario in {"reordered", "duplicate", "late"}:
        tick = {"reordered":0.25, "duplicate":2, "late":6}[scenario]
        start, end = events["scheduled_post_begin"], events["scheduled_post_ack"]
        e.verify(start["tick"] == tick and start["elapsed"] >= tick and start["monotonic"] <= end["monotonic"],
                 f"Scheduled {scenario} dispatch is ambiguous or early.")
        if scenario == "late":
            e.verify(start["monotonic"] > events["deadline_saved"]["monotonic"], "Late write preceded saved deadline.")
        else:
            bound = 1 if scenario == "reordered" else 5
            e.verify(end["elapsed"] < bound, f"{scenario} acknowledgment at +{end['elapsed']:.6f}s missed +{bound}.")
        e.observations["scheduled_ack_elapsed"] = end["elapsed"]
        sent, = [x for x in xs if x["channel"] == "delivery" and x["method"] == "POST"]
        e.verify(start["utc"] <= sent["started"] <= sent["ended"] <= end["utc"],
                 f"{scenario} exchange is outside its clock receipt.")
    if scenario in {"missing","late"} and mode != "off":
        a, b = events["recovery_post_begin"], events["recovery_post_ack"]
        e.verify(a["elapsed"] >= 1 and a["monotonic"] <= b["monotonic"], "Recovery started before grace or is unordered.")
        cutoff = zero["cutoff_monotonic"]
        e.verify(not (a["monotonic"] <= cutoff <= b["monotonic"]), "Recovery write straddled cutoff.")
        e.observations["recovery_ack_elapsed"] = b["elapsed"]
        rec, = [x for x in xs if x["channel"] == "recovery" and x["method"] == "POST"]
        e.verify(a["utc"] <= rec["started"] <= rec["ended"] <= b["utc"], "Recovery exchange outside its clock receipt.")
    for x in xs:
        if x["method"] != "POST":
            continue
        for snap in ("deadline","final"):
            left, right = events[snap+"_begin"]["utc"], events[snap+"_saved"]["utc"]
            e.verify(x["ended"] < left or x["started"] > right, f"Receiver POST coincided with held {snap} read.")
    return events


def audit_deliveries(e, scenario, mode, xs, fixtures, source, gate):
    firsts = [x for x in xs if x["channel"] == "receiver" and x["method"] == "POST"]
    scheduled = [x for x in xs if x["channel"] == "delivery" and x["method"] == "POST"]
    network_names = [e for e in ADMISSIONS if e != THREE or scenario in ("normal","duplicate")]
    e.verify(len(firsts) == len(network_names), "Original sequence length differs from scheduled input.")
    first_ids = {}
    for sent, admission in zip(firsts, network_names):
        e.verify(sent["sent"] == fixtures[admission], f"Input bytes for original {admission} differ.")
        e.check(sent["url"] == RECEIVE and sent["status"] == 200, f"Original {admission} was not accepted.")
        first_ids[admission] = sent["headers"]["X-Request-ID"]
    initial = [x for x in xs if x["channel"] == "receiver" and x["method"] == "GET" and x["started"] < firsts[0]["started"]]
    empty_ids = []
    for x in initial:
        url = urlsplit(x["url"])
        params = parse_qs(url.query)
        if url.path == LOOKUP and params.get("patientId") == [PATIENT] and "notificationResourceId" in params:
            empty_ids.extend(params["notificationResourceId"])
            e.verify(x["status"] == 204 and not x["body"], "An initial admission was not empty.")
    e.verify(sorted(empty_ids) == sorted(ADMISSIONS), "Fresh four-admission readback incomplete.")
    expected_scheduled = scenario in {"reordered","duplicate","late"}
    e.verify(len(scheduled) == int(expected_scheduled), "Scheduled delivery count differs.")
    if scheduled:
        x, = scheduled
        e.verify(x["url"] == RECEIVE and x["sent"] == fixtures[THREE], "Scheduled third payload changed.")
        expect_dup = scenario == "duplicate" or (scenario == "late" and mode != "off")
        e.check(x["status"] == (422 if expect_dup else 200), f"Unexpected scheduled HTTP {x['status']}.")
        if expect_dup:
            e.check(duplicate(x["body"]), "Duplicate-specific OperationOutcome lacks expected key or constraint.")
        else:
            first_ids[THREE] = x["headers"]["X-Request-ID"]
        e.observations["scheduled_http"] = x["status"]
    runtime = [x for x in xs if x["channel"] == "recovery"]
    gets = [x for x in runtime if x["method"] == "GET" and urlsplit(x["url"]).netloc]
    writes = [x for x in runtime if x["method"] == "POST"]
    need = scenario in {"missing","late"} and mode != "off"
    e.check(len(gets) == int(need) and len(writes) == int(need),
            f"Observed {len(gets)} gap queries and {len(writes)} recovery posts, expected {int(need)} each.")
    if need and gets and writes:
        get, write = gets[0], writes[0]
        route = gate["replay_path" if mode == "replay" else "search_path"]
        e.check(get["url"] == gate["base_url"] + "/" + route and get["status"] == 200,
                f"Unexpected {mode} recovery source request or status.")
        bundle = json.loads(get["body"])
        items = [x["resource"] for x in bundle["entry"] if x["resource"].get("resourceType") == "Encounter"]
        if mode == "search":
            e.check(len(items) == 4 and {x["id"] for x in items} == set(ADMISSIONS), "Search candidates differ.")
        else:
            e.check(len(items) == 1, "Replay returned multiple Encounter candidates.")
            args = [v for v in bundle["entry"][0]["resource"]["parameter"] if v["name"] == "notification-event"]
            e.check(len(args) == 1 and [v["valueString"] for v in args[0]["part"] if v["name"]=="event-number"] == ["3"],
                    "Replay did not return exactly event 3.")
        matches = [x for x in items if x["id"] == THREE]
        e.check(matches == [source] and encounter(json.loads(write["sent"])) == source,
                "Recovered source, forwarded record and prerequisite reference differ.")
        e.check(write["url"] == RECEIVE and write["status"] == 200, "Recovery notice was not accepted.")
        score_reads = [x for x in xs if x["channel"] == "scoring" and x["method"] == "GET"]
        e.verify(len(score_reads) == 1 and score_reads[0]["status"] == 200 and
                 json.loads(score_reads[0]["body"]) == source, "Scoring reference unavailable or differs from live source.")
        first_ids[THREE] = write["headers"]["X-Request-ID"]
    e.check(len(e.ids) == len(set(e.ids)) and all(e.ids), "A delivery reused a request ID.")
    return first_ids


def audit_records(e, scenario, mode, phase, xs, first_ids, fixtures, source):
    reference = {k: encounter(json.loads(v)) for k, v in fixtures.items()}
    recovered = scenario in {"missing","late"} and mode != "off"
    if recovered:
        reference[THREE] = source
    if mode == "off" and (scenario == "missing" or (scenario == "late" and phase == "deadline")):
        del reference[THREE]
    rel = e.observations[phase+"_raw"]
    x, = [x for x in xs if x["raw"].relative_to(e.folder).as_posix() == rel]
    if x["status"] == 204 and not x["body"]:
        rows = []
    elif x["status"] == 200:
        rows = json.loads(x["body"])
        if not isinstance(rows, list):
            raise ValueError("Patient snapshot does not contain a record array.")
    else:
        raise ValueError(f"Unreadable {phase} patient response, HTTP {x['status']}.")
    keyed = Counter(row.get("notificationResourceId") for row in rows)
    e.observations[phase+"_three_rows"] = keyed[THREE]
    e.observations[phase+"_total_rows"] = len(rows)
    e.check(keyed == Counter(reference.keys()), f"{phase} counts {dict(keyed)}, expected {dict.fromkeys(reference,1)}.")
    observed = {}
    for row in rows:
        k = row.get("notificationResourceId")
        if k not in reference:
            continue
        e.check(row.get("fhirServerBaseUrl") == BASE and row.get("patientId") == PATIENT and
                row.get("notificationResourceType") == "Encounter" and row.get("triggerEvent") == "encounter-start",
                f"{phase} identity tuple differs for {k}.")
        e.check(bool(row.get("id")) and row.get("xRequestId") == first_ids.get(k) and bool(first_ids.get(k)),
                f"{phase} row/request identity differs for {k}.")
        try:
            bundle = json.loads(row["notificationData"])
            nested = comparable(encounter(bundle))
        except (ValueError, KeyError, TypeError, ParseError) as error:
            e.fail.append(f"{phase} stored content for {k} is not a complete Encounter: {error}")
            continue
        e.check(bundle["resourceType"] == "Bundle" and nested == comparable(reference[k]),
                f"{phase} complete Encounter differs for {k}.")
        observed[k] = (row.get("id"), row.get("xRequestId"), nested)
    return observed


def audit_trial(folder, scenario, mode, fixtures, source, gate, migrations):
    e = Evidence(folder)
    e.phase("deployment", e.deployment, migrations)
    xs = e.phase("exchanges", e.exchanges)
    if xs is None:
        return e
    e.observations["receiver_exchanges"] = sum(not urlsplit(x["url"]).netloc for x in xs)
    e.observations["source_exchanges"] = len(xs) - e.observations["receiver_exchanges"]
    evs = e.phase("clock", audit_clock, e, scenario, mode, xs)
    ids = e.phase("inputs", audit_deliveries, e, scenario, mode, xs, fixtures, source, gate)
    if ids is None:
        return e
    stored = {key: e.phase(key, audit_records, e, scenario, mode, key, xs, ids, fixtures, source)
              for key in ("deadline", "final")}
    if stored["deadline"] is not None and stored["final"] is not None:
        for admission in stored["deadline"].keys() & stored["final"].keys():
            e.check(stored["deadline"][admission] == stored["final"][admission],
                    f"{admission} original row identity or complete content changed between snapshots.")
        if scenario == "duplicate" and THREE in stored["deadline"]:
            def compare_prior():
                prior, = [x for x in xs if x["channel"] == "receiver" and x["method"] == "GET"
                          and x["status"] == 200 and parse_qs(urlsplit(x["url"]).query).get("notificationResourceId") == [THREE]]
                row, = json.loads(prior["body"])
                prior_row = (row["id"], row["xRequestId"], comparable(encounter(json.loads(row["notificationData"]))))
                e.check(prior_row == stored["deadline"][THREE], "The duplicate replaced its originally stored admission row.")
            e.phase("prior duplicate row", compare_prior)
    if evs:
        e.observations["window_begin_utc"] = evs["notice_4_acknowledged"]["utc"]
        e.observations["window_end_utc"] = evs["final_saved"]["utc"]
    return e


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, help="Write a separate JSON report outside the preserved run.")
    args = parser.parse_args()
    run = args.run.resolve()
    if args.output and args.output.resolve().is_relative_to(run):
        parser.error("Keep the audit report outside the raw run.")
    result = read(run / "result.json")
    fixtures = {admission: (KIT / "fixtures" / f"admission-{i}.json").read_bytes()
                for i, admission in enumerate(ADMISSIONS,1)}
    migration_input = read(KIT / "runtime/migrations.json")
    migrations = [(v["path"],v["git_blob"]) for v in migration_input]
    records = {}
    issues = []
    archive = {}
    try:
        archive = verify_archive(run)
    except (*RAW_ERRORS, BadZipFile) as exc:
        issues.append("Archive integrity: " + str(exc))
    try:
        gate, source = read_gate(run, encounter(json.loads(fixtures[THREE])))
    except RAW_ERRORS as exc:
        gate, source = {}, None
        issues.append("Source prerequisites: " + str(exc))
    used = set()
    all_post_ids = set()
    intervals = []
    production = []
    not_run = []
    expected_trial_ids = set()
    for scenario, mode, repetition in product(CASES,MODES,(1,2,3)):
        key = f"{scenario}-{mode}-r{repetition}"
        expected_trial_ids.add(key)
        folder = run / "trials" / scenario / f"r{repetition}" / mode
        runner = (result.get("trials") or {}).get(key,{}).get("result")
        if runner is None and not folder.exists():
            not_run.append(key)
            records[key] = dict(runner=None, independent="NOT RUN", failures=[], unverified=[],
                                reason="No deployment, delivery evidence or case receipt exists.")
            continue
        e = audit_trial(folder,scenario,mode,fixtures,source,gate,migrations)
        for role, identity in e.deploy_ids.items():
            if identity in used:
                e.unknown.append(f"Reused {role} identity {identity}.")
            used.add(identity)
        if set(e.ids) & all_post_ids:
            e.fail.append("A sender request ID was reused in a different trial.")
        all_post_ids.update(e.ids)
        if "window_begin_utc" in e.observations:
            intervals.append((e.observations["window_begin_utc"],e.observations["window_end_utc"],key))
        if source is None or not archive:
            e.unknown.append("Global source or evidence integrity could not be established.")
        try:
            score = read(folder / "score.json")
            e.verify(score["result"] == runner, "Per-trial and consolidated verdicts differ.")
        except RAW_ERRORS as exc:
            e.unknown.append("Missing or unreadable trial score: " + str(exc))
        # An invalid schedule makes interpretation for the intended case unverified.
        audit = "UNVERIFIED" if e.unknown else "FAIL" if e.fail else "PASS"
        if runner != audit:
            issues.append(f"{key}: runner {runner}, independent audit {audit}.")
        if runner in {"FAIL","UNVERIFIED"} or audit != "PASS":
            production.append(key)
        records[key] = dict(runner=runner, independent=audit, failures=e.fail, unverified=e.unknown,
                            deployment=e.deploy_ids, observations=e.observations,
                            raw_directory=folder.relative_to(run).as_posix())
    actual_ids = set((result.get("trials") or {}).keys())
    if actual_ids != expected_trial_ids:
        issues.append(f"Trial inventory differs. Missing {sorted(expected_trial_ids-actual_ids)}, unexpected {sorted(actual_ids-expected_trial_ids)}.")
    aggregate = dict(Counter(value["runner"] for value in records.values() if value["runner"] is not None))
    if aggregate != result.get("counts", {}):
        issues.append(f"Top-level score counts differ from the individual receipts: {aggregate}.")
    intervals.sort()
    overlaps = [(a[2],b[2]) for a,b in zip(intervals,intervals[1:]) if a[1] > b[0]]
    if overlaps:
        issues.append(f"Measured trial windows overlap: {overlaps}.")
    counts = dict(Counter(t["independent"] for t in records.values()))
    report = dict(run=run.name, archive=archive, counts=counts, global_findings=issues,
                  flagged=production, not_run=not_run, trials=records, distinct_post_request_ids=len(all_post_ids))
    if args.output:
        args.output.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k != "trials"},indent=2))
    return 2 if issues or production else 0


if __name__ == "__main__":
    raise SystemExit(main())
