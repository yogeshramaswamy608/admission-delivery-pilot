"""Run the H1 receiver check from an extracted kit on a private test computer."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent
DEPLOYMENT_EVIDENCE = {"build.json", "migration.json", "state.json", "progress.jsonl", "compose.json"}
STAGING_EVIDENCE = {"stage-result.json", "cache-probe.json", "cache-probe.log", "cache-probe-outcome.json"}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_kit():
    manifest = ROOT / "SHA256SUMS"
    seen = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        expected, name = line.split("  ", 1)
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT) or name in seen or digest(path) != expected:
            raise ValueError(f"Kit file failed its integrity check: {name}")
        seen.add(name)
    required = {"start_h1.py", "run_h1.py", "stage_validator.py", "runtime/start.py", "receiver_check.py",
                "validation/run_official_validator.py", *(f"fixtures/admission-{n}.json" for n in range(1, 5)),
                "validation/terminology/codesystem-subscription-notification-type.json",
                "validation/terminology/valueset-subscription-notification-type.json"}
    if not required.issubset(seen):
        raise ValueError("The integrity manifest omits required inputs or programs.")
    return digest(manifest)


def call(args, log_path):
    with log_path.open("x", encoding="utf-8") as log:
        proc = subprocess.run(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False)
    return proc.returncode


def review_options(attempt_path, review_path, kit_hash):
    if attempt_path is None and review_path is None:
        return []
    if attempt_path is None or review_path is None:
        raise ValueError("Use --review-attempt and --warning-review together.")
    previous = attempt_path.resolve()
    prior = json.loads((previous / "summary.json").read_text(encoding="utf-8"))
    if prior.get("kit_manifest_sha256") != kit_hash or prior.get("result") != "BLOCKED":
        raise ValueError("Select the blocked attempt from this exact kit revision.")
    flagged = list((previous / "tests").glob("*/work/validation/official-run-*/issues-requiring-review.json"))
    if len(flagged) != 1 or not review_path.is_file():
        raise ValueError("Review requires one original set of flagged reports and the separate written review file.")
    return ["--review-run", str(flagged[0].parent), "--warning-review", str(review_path.resolve())]


def result_archive(attempt):
    target = attempt.parent / f"H1-evidence-{attempt.name}.zip"
    members = [attempt / "summary.json", ROOT / "SHA256SUMS"]
    members.extend(attempt.glob("*.log"))
    members.extend(path for path in (attempt / "deployment").glob("*") if path.name in DEPLOYMENT_EVIDENCE)
    members.extend(path for path in (attempt / "staging").glob("*") if path.name in STAGING_EVIDENCE)
    tests = attempt / "tests"
    if tests.is_dir():
        members.extend(path for path in tests.rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    with ZipFile(target, "x", compression=ZIP_DEFLATED) as archive:
        for path in sorted(members):
            archive.write(path, "kit-SHA256SUMS" if path == ROOT / "SHA256SUMS" else path.relative_to(attempt))
    return target


def run():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--dry-run", action="store_true", help="Verify kit files and preview. No downloads, services or admission deliveries.")
    cli.add_argument("--review-attempt", type=Path, help="Earlier attempt directory from this kit with flagged official reports.")
    cli.add_argument("--warning-review", type=Path, help="Separate evidence-backed review of the exact reported diagnostics.")
    args = cli.parse_args()
    if sys.version_info < (3, 11):
        cli.exit(2, "BLOCKED: install Python 3.11 or later on the test computer.\n")
    try:
        kit_hash = verify_kit()
        review = review_options(args.review_attempt, args.warning_review, kit_hash)
    except (ValueError, OSError, TypeError, AttributeError) as exc:
        cli.exit(2, f"BLOCKED before loading kit programs: {exc}\n")
    if args.dry_run:
        for entry in ["stage_validator.py", "run_h1.py"]:
            result = subprocess.run([sys.executable, "-B", str(ROOT / entry), "--dry-run"], cwd=ROOT, check=False)
            if result.returncode:
                return 2
        print("Preview only. A live run stages the validator, starts a private receiver and database, "
              "validates all four files, executes the eleven receiver exchanges, and packages the evidence.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    attempt = ROOT / "attempts" / f"{stamp}-{uuid4().hex[:10]}"
    attempt.mkdir(parents=True)
    summary = {"result": "BLOCKED", "stage": "prerequisites", "started_utc": stamp,
               "kit_manifest_sha256": kit_hash, "scope": "H1 receiver baseline"}
    python = [sys.executable, "-B"]
    try:
        prerequisites = ("docker",) if review else ("java", "docker")
        missing = [name for name in prerequisites if not shutil.which(name)]
        if missing:
            raise ValueError("Missing on this test computer: " + ", ".join(missing))
        if call(["docker", "compose", "version"], attempt / "compose-version.log"):
            raise ValueError("Docker Compose is unavailable. See compose-version.log.")
        if review:
            summary["reviewed_attempt"] = args.review_attempt.resolve().name
        else:
            summary["stage"] = "stage_validator"
            if call([*python, "stage_validator.py", "--report-dir", str(attempt / "staging")], attempt / "stage-validator.log"):
                raise ValueError("Validator staging stopped. See stage-validator.log.")
        summary["stage"] = "start_receiver"
        deployment = attempt / "deployment"
        if call([*python, "runtime/start.py", "--output", str(deployment)], attempt / "start-receiver.log"):
            raise ValueError("Receiver setup stopped. See deployment/state.json and start-receiver.log.")
        build = deployment / "build.json"
        receipt = json.loads(build.read_text(encoding="utf-8"))
        summary["stage"] = "validate_and_test"
        tests = attempt / "tests"
        status = call([*python, "run_h1.py", "--receiver", receipt["receiver_url"], "--build-record", str(build),
                       "--jar", "tools/validator_cli.jar", "--package", "tools/package.r4.tgz",
                       "--java-home", "tools/java-home", "--offline-docker",
                       "--evidence-dir", str(tests), *review], attempt / "run-test.log")
        reports = list(tests.glob("*/result.json"))
        if len(reports) != 1:
            raise ValueError("Missing a unique top-level run result. See run-test.log.")
        result = json.loads(reports[0].read_text(encoding="utf-8"))
        summary["test_result"] = str(reports[0].relative_to(attempt))
        if status or result.get("verdict") != "PASS":
            summary["result"] = "STOPPED" if result.get("verdict") == "STOPPED" else "BLOCKED"
            raise ValueError(result.get("detail") or "Read the test result and the original reports.")
        summary.update(result="PASS", stage="complete", reason="Four profile checks and the full live receiver sequence passed.")
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        summary["reason"] = str(exc)
    summary["ended_utc"] = datetime.now(timezone.utc).isoformat()
    (attempt / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    evidence = result_archive(attempt)
    print(f"{summary['result']}: {summary['reason']}\nEvidence: {evidence}")
    return 0 if summary["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(run())
