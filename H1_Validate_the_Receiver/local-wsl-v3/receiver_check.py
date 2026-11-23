"""Execute H1 against an isolated, identified local eCRNow 4.0.4 receiver."""
import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
from urllib.parse import urlencode, urlsplit
from uuid import uuid4
from xml.etree.ElementTree import canonicalize


ROOT = Path(__file__).resolve().parent
PROFILE = "http://hl7.org/fhir/uv/subscriptions-backport/StructureDefinition/backport-subscription-notification-r4"
PIN = "89da654471b651dd70592f371e577fffa3c5f04e"
IMAGE = "28a195c6138316988689adf21afcc2a239bf1f95dc9bc0b52e87fb113adc2976"
PATIENT = "drill-patient-1"


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def utc():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_json(path):
    return json.loads(path.read_bytes())


def fixtures():
    return [(ROOT / "fixtures" / f"admission-{n}.json") for n in range(1, 5)]


def check_profile_reports(paths):
    manifest = ROOT / "validation" / "official_validation.json"
    require(manifest.is_file(), "Full profile validation reports are pending.")
    data = load_json(manifest)
    require(data["profile"] == PROFILE and data["package_version"] == "1.1.0", "Incorrect validation profile or package version.")
    records = {item["filename"]: item for item in data["results"]}
    require(len(records) == len(data["results"]) == len(paths), "Expected four distinct official validation results.")
    for path in paths:
        item = records[path.name]
        report = (manifest.parent / item["report"]).resolve()
        require(report.is_relative_to(manifest.parent.resolve()), "Validation report path is outside the evidence directory.")
        require(item["passed"] is True and report.is_file(), f"Missing passing report for {path.name}.")
        require(item["report_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest(), f"Validation report changed for {path.name}.")
        require(item["sha256"].lower() == hashlib.sha256(path.read_bytes()).hexdigest(), f"Validated bytes differ for {path.name}.")


def expected_encounter(bundle):
    return bundle["entry"][1]["resource"]


def comparable(record):
    # HAPI can reserialize XHTML while retaining the same narrative and attributes.
    record = dict(record)
    if "text" in record and "div" in record["text"]:
        record["text"] = dict(record["text"])
        record["text"]["div"] = canonicalize(record["text"]["div"], rewrite_prefixes=True)
    return record


def check_row(body, expected):
    records = json.loads(body)
    require(isinstance(records, list) and len(records) == 1, "Expected exactly one stored row for the third admission.")
    row = records[0]
    require(row["patientId"] == PATIENT and row["notificationResourceId"] == expected["id"], "Stored identity differs.")
    require(row["notificationResourceType"] == "Encounter" and row["triggerEvent"] == "encounter-start", "Stored type or trigger differs.")
    stored = json.loads(row["notificationData"])
    require(stored["resourceType"] == "Bundle", "Stored notification is not a Bundle.")
    encounters = [e["resource"] for e in stored["entry"] if e.get("resource", {}).get("resourceType") == "Encounter"]
    require(len(encounters) == 1 and comparable(encounters[0]) == comparable(expected), "Complete stored Encounter differs.")


def check_duplicate(body):
    result = json.loads(body)
    require(result["resourceType"] == "OperationOutcome" and any(
        issue.get("severity") == "error" and issue.get("code") == "invalid"
        and "Unique Constraint Violation" in issue.get("details", {}).get("text", "")
        for issue in result["issue"]
    ), "The 422 response does not identify the duplicate constraint.")


def request(origin, evidence, label, method, path, expected_status, payload=None):
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers.update({"Content-Type":"application/fhir+json", "X-Request-ID":str(uuid4())})
    details = {"method":method, "path":path, "headers":headers, "expect":expected_status, "start_utc":utc()}
    if payload is not None:
        details["input_sha256"] = hashlib.sha256(payload).hexdigest()
        (evidence / (label + ".request.json")).write_bytes(payload)
    meta = evidence / (label + ".json")
    save_json(meta, details)
    transport = http.client.HTTPSConnection if origin.scheme == "https" else http.client.HTTPConnection
    connection = transport(origin.hostname, origin.port, timeout=15)
    try:
        connection.request(method, origin.path.rstrip("/") + path, payload, headers)
        reply = connection.getresponse()
        raw = reply.read()
        (evidence / (label + ".response")).write_bytes(raw)
        details.update(status=reply.status, end_utc=utc(), content_type=reply.getheader("Content-Type"))
        save_json(meta, details)
        require(reply.status == expected_status, f"{label}: expected HTTP {expected_status}, observed {reply.status}.")
        return raw
    finally:
        connection.close()


def lookup(admission):
    return "/api/notificationContexts?" + urlencode({"patientId":PATIENT, "notificationResourceId":admission})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receiver", help="Base URL of the isolated local test deployment, including application context if used.")
    parser.add_argument("--build-record", type=Path, help="Deployment evidence for the running receiver, containing its exact commit or image digest.")
    parser.add_argument("--dry-run", action="store_true", help="Read fixtures and print the sequence without performing network I/O.")
    args = parser.parse_args()
    paths = fixtures()
    payloads = [p.read_bytes() for p in paths]
    encounters = [expected_encounter(json.loads(p)) for p in payloads]
    for n, enc in enumerate(encounters, 1):
        require(enc["id"] == f"drill-adm-{n}" and enc["subject"]["reference"] == "Patient/"+PATIENT, "Unexpected test identity.")
    if args.dry_run:
        print("DRY RUN ONLY: four clean lookups (204), four receipts (200), inspect admission 3, resend the third (duplicate 422), inspect it again.")
        return 0
    results = ROOT / "runs"
    results.mkdir(exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=results))
    try:
        check_profile_reports(paths)
        require(args.receiver and args.build_record, "An identified local receiver and its deployment record are required.")
        origin = urlsplit(args.receiver)
        require(origin.scheme in ("http","https") and origin.hostname in ("127.0.0.1","::1","localhost")
                and not (origin.username or origin.password or origin.fragment or origin.query), "Use the designated isolated local test receiver.")
        deployment = args.build_record.read_text(encoding="utf-8")
        require(PIN in deployment or IMAGE in deployment, "The deployment record does not identify the pinned release.")
        save_json(evidence/"inputs.json", {"time_utc":utc(), "receiver":args.receiver,
                  "deployment_record_sha256":hashlib.sha256(args.build_record.read_bytes()).hexdigest(),
                  "validated_inputs":{p.name:hashlib.sha256(b).hexdigest() for p,b in zip(paths,payloads)}})
        for n, enc in enumerate(encounters, 1):
            request(origin, evidence, f"01-clean-{n}", "GET", lookup(enc["id"]), 204)
        for n, payload in enumerate(payloads, 1):
            request(origin, evidence, f"02-receive-{n}", "POST", "/api/receive-notification", 200, payload)
        third = encounters[2]
        check_row(request(origin, evidence, "03-before-repeat", "GET", lookup(third["id"]), 200), third)
        check_duplicate(request(origin, evidence, "04-repeat", "POST", "/api/receive-notification", 422, payloads[2]))
        check_row(request(origin, evidence, "05-after-repeat", "GET", lookup(third["id"]), 200), third)
    except (ValueError, KeyError, TypeError, IndexError, OSError, http.client.HTTPException) as exc:
        observation = "STOPPED" if list(evidence.glob("*.response")) else "NOT TESTED"
        save_json(evidence/"result.json", {"result":observation,"time_utc":utc(),"reason":str(exc)})
        print(observation, str(exc), evidence.name)
        return 2
    save_json(evidence/"result.json", {"result":"PASS","time_utc":utc(),"scope":"H1 only"})
    print("H1 PASS", evidence.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
