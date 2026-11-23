"""Run the preregistered delivery matrix and preserve every raw trial."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from ipaddress import ip_network
from itertools import product
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
H3 = ROOT.parent / "H3_Check_Recovery_at_Deadline"
sys.path.insert(0, str(H3))
import run_h3
from run_h3 import compare, receiver_check, RecoveryLayer, source_gate

SCENARIOS = ("normal", "missing", "late", "reordered", "duplicate")
MODES = ("off", "replay", "search")
REPEATS = (1, 2, 3)


def manifest():
    return [dict(id=f"{case}-{mode}-r{rep}", scenario=case, mode=mode, repetition=rep,
                 directory=f"trials/{case}/r{rep}/{mode}")
            for case, mode, rep in product(SCENARIOS, MODES, REPEATS)]


def reserve_subnets(docker, count):
    # Fresh Docker networks persist as evidence. Explicit small subnets avoid exhausting
    # Docker's limited pool of default bridges while leaving existing networks alone.
    ids = subprocess.check_output(docker + ["network", "ls", "-q"], text=True).split()
    existing = subprocess.check_output(docker + ["network", "inspect", "-f", "{{json .IPAM.Config}}", *ids], text=True)
    occupied = [ip_network(c["Subnet"], strict=False) for line in existing.splitlines()
                for c in (json.loads(line) or []) if c.get("Subnet")]
    routes = json.loads(subprocess.check_output(["ip", "-j", "-4", "route", "show", "table", "all"]))
    occupied += [ip_network(r["dst"], strict=False) for r in routes if r.get("dst") not in (None, "default")]
    chosen = []
    for private in ("10.225.0.0/16", "10.226.0.0/16", "172.20.0.0/16", "192.168.0.0/16"):
        for subnet in ip_network(private).subnets(new_prefix=28):
            if not any(other.version == 4 and subnet.overlaps(other) for other in occupied):
                chosen.append(str(subnet))
                if len(chosen) == count:
                    return chosen
    raise RuntimeError("Insufficient disjoint private subnets for the requested deployments.")


def provision(folder, subnet, prefix):
    runtime = folder / "runtime"
    runtime.mkdir(parents=True)
    for name in ("start.py", "migrations.json"):
        shutil.copyfile(compare.KIT / "runtime" / name, runtime / name)
    config = json.loads((compare.KIT / "runtime/compose.json").read_bytes())
    compare.require(config["networks"]["private"]["internal"] is True, "Expected isolated private network.")
    config["networks"]["private"]["ipam"] = {"config": [{"subnet": subnet}]}
    compare.save(runtime / "compose.json", config)
    with (folder / "start.log").open("xb") as log:
        subprocess.run(prefix + [sys.executable, "-B", str(runtime / "start.py"), "--output", str(folder / "deployment")],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    build = json.loads((folder / "deployment/build.json").read_bytes())
    compare.require(build["transport"]["local_engine"] == compare.PRIVATE_ENGINE and build["network"]["internal"]
                    and build["readiness"]["status"] == 204 and build["transport"]["tester_image"] == compare.TESTER,
                    "Unexpected deployment receipt.")
    return build


def stop(folder, docker):
    deploy = folder / "deployment"
    if not (deploy / "state.json").is_file() or not (deploy / "compose.json").is_file():
        return
    project = json.loads((deploy / "state.json").read_bytes())["project"]
    compare.require(re.fullmatch(r"h1-[0-9a-f]{32}", project), "Unknown deployment identity.")
    with (folder / "stop.log").open("xb") as log:
        subprocess.run(docker + ["compose", "-p", project, "-f", str(deploy / "compose.json"), "stop"],
                       stdout=log, stderr=subprocess.STDOUT, timeout=90, check=True)


def inputs(attempt, gate_path):
    run_h3.snapshot_inputs(attempt, gate_path)
    for path in (H3 / "run_h3.py", H3 / "trial.py", ROOT / "expected_results.md",
                 ROOT / "run_local.py", ROOT / "run_h4.py", ROOT / "trial_h4.py",
                 ROOT / "audit_h4.py", ROOT / "test_audit_h4.py", ROOT / "test_trial_h4.py"):
        dest = attempt / "inputs" / path.relative_to(ROOT.parent)
        dest.parent.mkdir(exist_ok=True, parents=True)
        shutil.copyfile(path, dest)


def test_cell(attempt, trials, gate, prefix, docker, summary, exercise):
    folders = [attempt / t["directory"] for t in trials]
    with ThreadPoolExecutor(max_workers=len(trials)) as pool:
        builds = [pool.submit(provision, f, t["subnet"], prefix) for f, t in zip(folders, trials)]
    # Provisioning and measurement are separate phases. Only one trial sends notices at a time.
    pending = set(folders)
    try:
        for folder, trial, build in zip(folders, trials, builds):
            try:
                observed = exercise(trial["scenario"], trial["mode"], folder, gate, docker, build.result(),
                                    receiver_check, RecoveryLayer)
                compare.require(observed["result"] in {"PASS", "FAIL", "UNVERIFIED"}, "Unknown verdict.")
            except Exception as error:
                observed = dict(result="UNVERIFIED", reasons=[f"{type(error).__name__}: {error}"])
            try:
                stop(folder, docker)
            except Exception as error:
                observed["cleanup_error"] = str(error)
                if observed["result"] == "PASS":
                    observed["result"] = "UNVERIFIED"
            pending.discard(folder)
            folder.mkdir(parents=True, exist_ok=True)
            compare.save(folder / "score.json", observed)
            summary["trials"][trial["id"]] = observed
            compare.save(attempt / "result.json", summary)
            print(f"{len(summary['trials']):02d}/45 {trial['id']} {observed['result']}", flush=True)
    finally:
        for folder in pending:
            stop(folder, docker)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    from trial_h4 import exercise
    run_h3.validate_inputs(args.gate)
    trials = manifest()
    compare.require(len(trials) == len({t["id"] for t in trials}) == 45, "Incomplete trial matrix.")
    if args.dry_run:
        print("45 unique trials. H4 driver imports, pinned source receipt and H1 kit verified.")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    attempt = ROOT / "runs" / f"{stamp}-{uuid4().hex[:8]}"
    attempt.mkdir(parents=True)
    summary = dict(status="RUNNING", started=compare.utc(), trials={})
    print(f"H4 evidence: {attempt}", flush=True)
    try:
        inputs(attempt, args.gate)
        gate = source_gate.verify_existing(args.gate, attempt / "prerequisites")
        compare.require(gate["result"] == "PASS", "Fresh source checks failed.")
        prefix = run_h3.docker_access()
        docker = prefix + ["docker", "--host", compare.PRIVATE_ENGINE]
        for trial, subnet in zip(trials, reserve_subnets(docker, len(trials))):
            trial["subnet"] = subnet
        compare.save(attempt / "plan.json", trials)
        for start in range(0, len(trials), len(REPEATS)):
            test_cell(attempt, trials[start:start+len(REPEATS)], gate, prefix, docker, summary, exercise)
        counts = Counter(s["result"] for s in summary["trials"].values())
        summary["counts"] = dict(counts)
        summary["status"] = "FAIL" if counts["FAIL"] else "UNVERIFIED" if counts["UNVERIFIED"] else "PASS"
    except (Exception, KeyboardInterrupt) as error:
        summary.update(status="BLOCKED", reason=f"{type(error).__name__}: {error}")
    summary["ended"] = compare.utc()
    compare.save(attempt / "result.json", summary)
    archive, entries = run_h3.archive_evidence(attempt, prefix="H4")
    print(f"{summary['status']}. Verified {entries} ZIP entries. {archive}", flush=True)
    report = attempt.parent / f"H4-{attempt.name}-audit.json"
    audited = subprocess.run([sys.executable, "-B", str(ROOT / "audit_h4.py"), str(attempt),
                              "--output", str(report)])
    print(f"Independent evidence review: {report}", flush=True)
    return 0 if summary["status"] == "PASS" and audited.returncode == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
