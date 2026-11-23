"""Run the three approved H3 deadline trials against fresh local receivers."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent
H2 = ROOT.parent / "H2_Compare_Recovery"
sys.path.insert(0, str(H2))
import compare

sys.path.insert(0, str(compare.KIT))
sys.path.insert(0, str(H2 / "source-server"))
import receiver_check
from recovery import RecoveryLayer
import source_gate


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_inputs(path):
    compare.check_kit()
    old = json.loads(path.read_bytes())
    for key, value in {
        "result": "PASS", "server_version": source_gate.VERSION,
        "server_commit": source_gate.COMMIT, "nuget_sha256": source_gate.PACKAGE_SHA256,
        "subscription_id": source_gate.SUB, "patient_id": source_gate.PATIENT,
        "replay_path": source_gate.REPLAY, "search_path": source_gate.SEARCH,
    }.items():
        compare.require(old.get(key) == value, f"Unexpected source receipt field: {key}")
    return old


def snapshot_inputs(attempt, gate_path):
    files = [ROOT / "expected_results.md", *sorted(ROOT.glob("*.py")),
             H2 / "compare.py", H2 / "recovery/__init__.py", H2 / "source-server/source_gate.py",
             H2 / "source-server/prepare_runtime.sh", compare.KIT / "SHA256SUMS",
             compare.KIT / "receiver_check.py", compare.KIT / "runtime/start.py",
             compare.KIT / "runtime/compose.json", compare.KIT / "runtime/migrations.json"]
    for source in files:
        target = attempt / "inputs" / source.relative_to(ROOT.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copyfile(gate_path, attempt / "inputs/source-setup-gate.json")


def docker_access():
    engine = ["docker", "--host", compare.PRIVATE_ENGINE, "version", "--format", "{{.Server.Version}}"]
    if subprocess.run(engine, capture_output=True).returncode == 0:
        return []
    subprocess.run(["sudo", "-v"], check=True)
    prefix = ["sudo", "-n"]
    subprocess.run(prefix + engine, capture_output=True, check=True)
    return prefix


def run_cases(attempt, gate, prefix, exercise, summary):
    for mode in compare.MODES:
        print(f"Testing {mode}", flush=True)
        try:
            score = compare.run_case(mode, attempt, gate, prefix, receiver_check, RecoveryLayer, exercise)
            compare.require(score["result"] in {"PASS", "FAIL", "UNVERIFIED"}, "Unknown case result")
        except Exception as error:
            # A failed deployment, read, cleanup, or scorer must retain its own evidence.
            folder = attempt / mode
            folder.mkdir(exist_ok=True)
            score = {"result": "UNVERIFIED", "reason": f"{type(error).__name__}: {error}"}
            score_path = folder / "score.json"
            if score_path.exists():
                score["trial_result_before_error"] = json.loads(score_path.read_bytes())
            compare.save(score_path, score)
        summary["cases"][mode] = score
        compare.save(attempt / "result.json", summary)
    results = {case["result"] for case in summary["cases"].values()}
    summary["status"] = "FAIL" if "FAIL" in results else "UNVERIFIED" if "UNVERIFIED" in results else "PASS"


def archive_evidence(attempt, prefix="H3"):
    paths = sorted(attempt.rglob("*"))
    for path in paths:
        compare.require(not path.is_symlink() and (path.is_file() or path.is_dir()),
                        f"Unexpected evidence file type: {path}")
    digests = {p.relative_to(attempt).as_posix(): file_hash(p) for p in paths if p.is_file()}
    inventory = attempt / "SHA256SUMS"
    with inventory.open("x", encoding="utf-8", newline="\n") as stream:
        stream.writelines(f"{digest}  {name}\n" for name, digest in digests.items())
    digests[inventory.name] = file_hash(inventory)
    archive_path = attempt.parent / f"{prefix}-{attempt.name}.zip"
    with ZipFile(archive_path, "x", compression=ZIP_DEFLATED) as archive:
        for name in digests:
            archive.write(attempt / name, name)
    with ZipFile(archive_path) as archive:
        compare.require(archive.namelist() == list(digests), "ZIP inventory mismatch")
        for name, expected in digests.items():
            with archive.open(name) as stream:
                archived = hashlib.file_digest(stream, "sha256").hexdigest()
            compare.require(archived == expected == file_hash(attempt / name), f"Evidence changed: {name}")
    return archive_path, len(digests)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", type=Path, required=True, help="Passing gate.json for the running local source.")
    parser.add_argument("--dry-run", action="store_true", help="Check code, kit and gate without starting trials.")
    args = parser.parse_args(argv)
    from trial import exercise
    gate_path = args.gate.resolve()
    old = validate_inputs(gate_path)
    if args.dry_run:
        print("Dry run verified H3 trial imports, H1 kit and pinned source receipt. Cases: off, replay, search.")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    attempt = ROOT / "runs" / (stamp + "-" + uuid4().hex[:8])
    attempt.mkdir(parents=True)
    summary = dict(started=compare.utc(), status="BLOCKED", source_gate=str(gate_path), cases={})
    print("H3 evidence:", attempt, flush=True)
    try:
        snapshot_inputs(attempt, gate_path)
        verified = source_gate.verify_existing(gate_path, attempt / "prerequisites")
        compare.require(verified["result"] == "PASS" and verified["base_url"] == old["base_url"]
                        and verified["subscription_id"] == old["subscription_id"], "Fresh source check failed.")
        run_cases(attempt, verified, docker_access(), exercise, summary)
    except (Exception, KeyboardInterrupt) as error:
        summary["reason"] = f"{type(error).__name__}: {error}"
    summary["ended"] = compare.utc()
    compare.save(attempt / "result.json", summary)
    archive, entries = archive_evidence(attempt)
    print(f"{summary['status']}. Verified {entries} ZIP entries. Evidence: {archive}")
    return 0 if summary["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
