"""Run the three approved H2 cases against fresh, private eCRNow receivers."""
import argparse
import base64
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent
KIT = ROOT.parent / "H1_Validate_the_Receiver" / "local-wsl-v3"
SAVED_MANIFEST = "01b7d43d8487a3735357eceed53284a097e4d066d8b80ce401db6689da540bec"
MODES = ("off", "replay", "search")
DELIVERED = (1, 2, 4)
WAIT_SECONDS = 1.0
PRIVATE_ENGINE = "unix:///var/run/docker.sock"
TESTER = "python@sha256:e8c69d86735f6df3efa06b324694fe51f0422785f83a3bd463052636c2421944"

# This client runs in the receiver's isolated network namespace. It receives only one
# request on stdin and returns the response without mounting the fixtures or the scorer.
CONTAINER_CLIENT = """
import base64, http.client, json, sys
r = json.load(sys.stdin)
c = http.client.HTTPConnection("127.0.0.1", 8081, timeout=15)
try:
    body = base64.b64decode(r["body"]) if r["body"] is not None else None
    c.request(r["method"], r["path"], body=body, headers=r["headers"])
    reply = c.getresponse()
    print(json.dumps([reply.status, reply.getheaders(), base64.b64encode(reply.read()).decode()]))
finally:
    c.close()
"""


def utc():
    return datetime.now(timezone.utc).isoformat()


def save(path, obj):
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_kit():
    manifest = (KIT / "SHA256SUMS").read_bytes()
    require(hashlib.sha256(manifest).hexdigest() == SAVED_MANIFEST, "H1 manifest changed.")
    for line in manifest.decode().splitlines():
        expected, name = line.split("  ", 1)
        # The recovery path never loads or hashes the withheld fixture.
        if name != "fixtures/admission-3.json":
            require(hashlib.sha256((KIT / name).read_bytes()).hexdigest() == expected, f"Changed H1 kit member: {name}")


class Exchanges:
    def __init__(self, folder, base_url, docker, container_id):
        self.folder = folder
        self.base = urlsplit(base_url)
        require(self.base.scheme == "http" and self.base.hostname == "127.0.0.1" and self.base.port
                and not self.base.username and not self.base.query and not self.base.fragment,
                "Expected the private loopback source from the gate.")
        self.docker = docker
        self.container_id = container_id
        self.counter = 0

    def begin(self, kind, method, url, headers, body=None):
        self.counter += 1
        stem = self.folder / f"{self.counter:02}-{kind}"
        meta = dict(method=method, url=url, headers=headers, started=utc())
        if body is not None:
            stem.with_suffix(".request").write_bytes(body)
            meta["request_sha256"] = hashlib.sha256(body).hexdigest()
        save(stem.with_suffix(".json"), meta)
        return stem, meta

    def finish(self, stem, meta, status, headers, body):
        stem.with_suffix(".response").write_bytes(body)
        meta.update(status=status, response_headers=headers, ended=utc())
        save(stem.with_suffix(".json"), meta)
        return status, body

    def source(self, url, *, kind="source"):
        target = urlsplit(url)
        require((target.scheme, target.netloc) == (self.base.scheme, self.base.netloc)
                and target.path.startswith(self.base.path.rstrip("/") + "/") and not target.fragment,
                "Source lookup left the checked tenant.")
        headers = {"Accept": "application/fhir+json"}
        stem, meta = self.begin(kind, "GET", url, headers)
        client = http.client.HTTPConnection(target.hostname, target.port, timeout=20)
        try:
            client.request("GET", target.path + ("?" + target.query if target.query else ""), headers=headers)
            reply = client.getresponse()
            return self.finish(stem, meta, reply.status, reply.getheaders(), reply.read())
        finally:
            client.close()

    def receiver(self, method, path, body=None, request_id=None):
        headers = {"Accept": "application/json"}
        if body is not None:
            headers.update({"X-Request-ID": request_id, "Content-Type": "application/fhir+json"})
        stem, meta = self.begin("receiver", method, path, headers, body)
        request = {"method": method, "path": path, "headers": headers,
                   "body": base64.b64encode(body).decode() if body is not None else None}
        cmd = self.docker + ["run", "--rm", "--pull=never", "--platform", "linux/amd64",
                             "--network", "container:" + self.container_id, "--cap-drop", "ALL",
                             "--security-opt", "no-new-privileges", "--read-only", "-i", TESTER,
                             "python", "-I", "-c", CONTAINER_CLIENT]
        result = subprocess.run(cmd, input=json.dumps(request).encode(), capture_output=True, timeout=45)
        if result.returncode:
            raise RuntimeError(f"Receiver client exit {result.returncode} during {stem.name}. Inspect deployment.")
        status, reply_headers, encoded = json.loads(result.stdout)
        return self.finish(stem, meta, status, reply_headers, base64.b64decode(encoded))

    def post(self, body, request_id):
        return self.receiver("POST", "/api/receive-notification", body, str(request_id))


def score_missing(mode, http, base_url, status, body, checker):
    if mode == "off":
        require(status == 204 and not body, "The unmodified baseline already has admission three.")
        return 0
    # Read the scoring reference only after recovery has finished forwarding.
    code, raw_source = http.source(base_url.rstrip("/") + "/Encounter/drill-adm-3", kind="score-source")
    require(code == 200 and status == 200, "Source reference or receiver verification failed.")
    source = json.loads(raw_source)
    fixture = json.loads((KIT / "fixtures/admission-3.json").read_bytes())["entry"][1]["resource"]
    clinical = dict(source)
    meta = dict(clinical.get("meta", {}))
    for key in ("versionId", "lastUpdated"):
        meta.pop(key, None)
    if not meta:
        clinical.pop("meta", None)
    else:
        clinical["meta"] = meta
    require(checker.comparable(clinical) == checker.comparable(fixture),
            "Source admission three differs from the original fixture beyond server version metadata.")
    checker.check_row(body, source)
    return 1


def exercise(mode, folder, gate, docker, build, checker, layer_type):
    patient = gate["patient_id"]
    require(patient == "drill-patient-1", "Gate identified the wrong patient.")
    exchanges = Exchanges(folder, gate["base_url"], docker, build["receiver"]["container_id"])
    layer = layer_type(gate["base_url"], gate["subscription_id"], patient, WAIT_SECONDS,
                       exchanges.source, exchanges.post)
    for number in range(1, 5):
        status, body = exchanges.receiver("GET", checker.lookup(f"drill-adm-{number}"))
        require(status == 204 and not body, "Receiver did not start empty.")
    for number in DELIVERED:
        notice = (KIT / "fixtures" / f"admission-{number}.json").read_bytes()
        status, _ = exchanges.post(notice, uuid4())
        require(status == 200, f"Original admission {number} was not accepted.")
        layer.observe_forwarded(notice, time.monotonic())
    gap = layer.gap()
    require(gap is not None and gap.event_number == 3, "The delivered sequence did not expose gap 3.")
    time.sleep(max(0, gap.eligible_at - time.monotonic()))
    verified_at = time.monotonic()
    require(verified_at >= gap.eligible_at, "Out-of-order grace period has not elapsed.")
    save(folder / "gap.json", dict(**asdict(gap), observed_after_wait=verified_at, wait_seconds=WAIT_SECONDS))
    for number in DELIVERED:
        status, body = exchanges.receiver("GET", checker.lookup(f"drill-adm-{number}"))
        require(status == 200, f"Original admission {number} was not retained.")
        notice = json.loads((KIT / "fixtures" / f"admission-{number}.json").read_bytes())
        checker.check_row(body, notice["entry"][1]["resource"])
    status, body = exchanges.receiver("GET", checker.lookup("drill-adm-3"))
    require(status == 204 and not body, "Withheld admission was present before recovery.")
    recovery = None
    if mode != "off":
        recovery = layer.recover(mode, time.monotonic())
        require(recovery is not None and recovery.receiver_status == 200, "Recovery did not deliver one notice.")
    status, body = exchanges.receiver("GET", checker.lookup("drill-adm-3"))
    rows = score_missing(mode, exchanges, gate["base_url"], status, body, checker)
    return {"result": "PASS", "admission_three_rows": rows,
            "recovery": asdict(recovery) if recovery is not None else None}


def run_case(mode, parent, gate, prefix, checker, layer_type, exercise_case=exercise):
    folder = parent / mode
    folder.mkdir()
    deploy = folder / "deployment"
    docker = prefix + ["docker", "--host", PRIVATE_ENGINE]
    try:
        with (folder / "start.log").open("xb") as log:
            subprocess.run(prefix + [sys.executable, "-B", str(KIT / "runtime" / "start.py"),
                                     "--output", str(deploy)], stdout=log, stderr=subprocess.STDOUT, check=True)
        build = json.loads((deploy / "build.json").read_bytes())
        require(build["transport"]["local_engine"] == PRIVATE_ENGINE and build["network"]["internal"]
                and build["readiness"]["status"] == 204 and build["transport"]["tester_image"] == TESTER,
                "Unexpected receiver deployment.")
        result = exercise_case(mode, folder, gate, docker, build, checker, layer_type)
        save(folder / "score.json", result)
        return result
    finally:
        # Retain volumes and evidence. Stop only services created for this individual case.
        state = deploy / "state.json"
        if state.is_file() and (deploy / "compose.json").is_file():
            project = json.loads(state.read_bytes())["project"]
            require(re.fullmatch(r"h1-[0-9a-f]{32}", project), "Unexpected test deployment identity.")
            with (folder / "stop.log").open("xb") as log:
                subprocess.run(docker + ["compose", "-p", project, "-f", str(deploy / "compose.json"), "stop"],
                               stdout=log, stderr=subprocess.STDOUT, timeout=90, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", type=Path, required=True, help="Passing prerequisite gate.json from the active source.")
    parser.add_argument("--dry-run", action="store_true", help="Check local code and receipt without starting receivers.")
    args = parser.parse_args()
    check_kit()
    sys.path.insert(0, str(KIT))
    import receiver_check
    from recovery import RecoveryLayer
    sys.path.insert(0, str(ROOT / "source-server"))
    from source_gate import verify_existing

    old = json.loads(args.gate.read_bytes())
    require(old["result"] == "PASS" and old["server_version"] == "2026.708.1348"
            and old["server_commit"] == "f04a1d4bdcf0265eb401b98eb0647a4ff373813e",
            "Select the verified pinned source's passing gate.")
    if args.dry_run:
        print("Dry run verified the kit and selected gate. Cases: off, replay, search.")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    attempt = ROOT / "runs" / (stamp + "-" + uuid4().hex[:8])
    attempt.mkdir(parents=True)
    summary = dict(started=utc(), source_gate=str(args.gate.resolve()), cases={}, status="BLOCKED")
    print("H2 comparison evidence:", attempt, flush=True)
    try:
        gate_path = attempt / "prerequisites"
        verify_existing(args.gate.resolve(), gate_path)
        verified = json.loads((gate_path / "gate.json").read_bytes())
        require(verified["result"] == "PASS" and verified["base_url"] == old["base_url"]
                and verified["subscription_id"] == old["subscription_id"], "Fresh prerequisite check failed.")
        docker = subprocess.run(["docker", "--host", PRIVATE_ENGINE, "version", "--format", "{{.Server.Version}}"],
                                capture_output=True)
        prefix = []
        if docker.returncode:
            subprocess.run(["sudo", "-v"], check=True)
            prefix = ["sudo", "-n"]
        for mode in MODES:
            print(f"Testing {mode}", flush=True)
            try:
                outcome = run_case(mode, attempt, verified, prefix, receiver_check, RecoveryLayer)
            except (ValueError, KeyError, RuntimeError) as error:
                if not (attempt / mode / "deployment" / "build.json").is_file():
                    raise
                outcome = {"result": "STOPPED", "reason": str(error)}
                save(attempt / mode / "score.json", outcome)
            summary["cases"][mode] = outcome
            save(attempt / "result.json", summary)
        summary["status"] = "PASS" if all(case["result"] == "PASS" for case in summary["cases"].values()) else "STOPPED"
        if summary["status"] == "STOPPED":
            summary["reason"] = "At least one case failed its comparison checks. See the preserved score files."
    except (OSError, ValueError, KeyError, RuntimeError, http.client.HTTPException,
            subprocess.SubprocessError, KeyboardInterrupt) as error:
        summary["reason"] = str(error) or type(error).__name__
    summary["ended"] = utc()
    save(attempt / "result.json", summary)
    files = sorted(path for path in attempt.rglob("*") if path.is_file())
    inventory = attempt / "SHA256SUMS"
    inventory.write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(attempt)}\n"
                                 for path in files), encoding="utf-8")
    archive_path = attempt.parent / f"H2-{attempt.name}.zip"
    with ZipFile(archive_path, "x", compression=ZIP_DEFLATED) as archive:
        for path in [*files, inventory]:
            archive.write(path, path.relative_to(attempt))
    print(summary["status"], summary.get("reason", "All three cases matched."), "\nEvidence:", archive_path)
    return 0 if summary["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
