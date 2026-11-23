"""Verify this distribution and run the pinned admission delivery interoperability pilot."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
H2 = "H2_Compare_Recovery"
H3 = "H3_Check_Recovery_at_Deadline"
H4 = "H4_Delivery_Cases"
SOURCE = ROOT / H2 / "source-server"
OUTPUTS = {"run-history", H4 + "/runs", H2 + "/source-server/runs"}
VERSION = "v1.0.2"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(root):
    manifest = root / "SHA256SUMS"
    if manifest.is_symlink():
        raise ValueError("Unexpected linked package manifest.")
    expected = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        checksum, name = line.split("  ", 1)
        path = PurePosixPath(name)
        if (re.fullmatch(r"[0-9a-f]{64}", checksum) is None or not path.parts or path.is_absolute()
                or ".." in path.parts or name != path.as_posix() or "\\" in name or name in expected
                or name == "SHA256SUMS" or any(o == name or name.startswith(o + "/") for o in OUTPUTS)):
            raise ValueError(f"Invalid distribution manifest entry {name!r}.")
        expected[name] = checksum
    found = set()
    def inspect(directory):
        for p in directory.iterdir():
            name = p.relative_to(root).as_posix()
            if p.is_symlink():
                raise ValueError(f"Unexpected link {name}.")
            if name in OUTPUTS:
                if not p.is_dir():
                    raise ValueError(f"Expected output directory at {name}.")
            elif p.is_dir():
                inspect(p)
            elif name != "SHA256SUMS":
                if not p.is_file() or name not in expected:
                    raise ValueError(f"Unexpected distribution file {name}.")
                if digest(p) != expected[name]:
                    raise ValueError(f"Distribution checksum mismatch for {name}.")
                found.add(name)
    inspect(root)
    if expected.keys() != found:
        raise ValueError("Missing distribution files: " + ", ".join(sorted(expected.keys() - found)))
    if (root / "VERSION").read_text().strip() != VERSION or not {"run.py", "README.md", "DECISIONS.md"} <= found:
        raise ValueError("The distribution identity is missing or differs from the launcher.")
    return len(found)


def offline_tests():
    for folder, args in ((ROOT, ["test_package"]), (ROOT / H2, ["discover", "-s", "recovery"]),
                         (ROOT / H3, ["discover"]), (ROOT / H4, ["discover"])):
        subprocess.run([sys.executable, "-B", "-m", "unittest", *args], cwd=folder, check=True)


def preflight():
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise ValueError("The live runner requires Linux on x86-64.")
    required = ("bash", "curl", "tar", "dpkg-deb", "sha256sum", "sha512sum", "ip", "ss", "docker")
    missing = [name for name in required if not shutil.which(name)]
    if missing:
        raise ValueError("Missing host programs: " + ", ".join(missing))
    sys.path.insert(0, str(ROOT / H3))
    import run_h3
    prefix = run_h3.docker_access()
    engine = prefix + ["docker", "--host", run_h3.compare.PRIVATE_ENGINE]
    subprocess.run(engine + ["compose", "version"], check=True)


def save(path, values):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def live_source(runtime, history):
    with (history / "source-setup.log").open("x", encoding="utf-8") as log:
        setup = subprocess.run(["bash", str(SOURCE / "prepare_runtime.sh"), str(runtime)],
                               cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        if setup.returncode:
            raise ValueError(f"Cold source download or verification failed. See {log.name}.")
        process = subprocess.Popen([sys.executable, "-B", str(SOURCE / "source_gate.py"),
                                    "--runtime", str(runtime)], cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            for line in process.stdout:
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
                if line.startswith("PASS: "):
                    gate = Path(line[6:].strip()).resolve()
                    if gate.parent.parent != SOURCE / "runs" or gate.name != "gate.json":
                        raise ValueError("Unexpected fresh source receipt location.")
                    yield gate
                    break
            else:
                raise ValueError(f"Fresh source prerequisite checks failed. See {log.name}.")
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=15)
                raise
            log.write(process.stdout.read())
            process.stdout.close()


def live(count):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    history = ROOT / "run-history" / (stamp + "-" + uuid4().hex[:8])
    history.mkdir(parents=True)
    record = dict(version=VERSION, started=now(), status="STARTING",
                  manifest_sha256=digest(ROOT / "SHA256SUMS"), verified_files=count)
    status = history / "launch.json"
    save(status, record)
    print("Package launch evidence:", history, flush=True)
    try:
        preflight()
        with tempfile.TemporaryDirectory(prefix="admission-pilot-source-") as empty_runtime:
            print("Preparing a new source from its three pinned downloads.", flush=True)
            with live_source(Path(empty_runtime), history) as gate:
                record.update(status="RUNNING", source=gate.relative_to(ROOT).as_posix())
                save(status, record)
                command = [sys.executable, "-B", str(ROOT / H4 / "run_h4.py"), "--gate", str(gate)]
                with (history / "run.log").open("x", encoding="utf-8") as log:
                    with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True) as proc:
                        for line in proc.stdout:
                            log.write(line)
                            log.flush()
                            print(line, end="", flush=True)
                            if line.startswith("H4 evidence: "):
                                attempt = Path(line.removeprefix("H4 evidence: ").strip()).resolve()
                                if attempt.parent != ROOT / H4 / "runs":
                                    raise ValueError("Unexpected trial evidence location.")
                                record["attempt"] = attempt.relative_to(ROOT).as_posix()
                                save(status, record)
                        code = proc.wait()
        if "attempt" in record:
            attempt = ROOT / record["attempt"]
            if (attempt / "result.json").is_file():
                record["trial_status"] = json.loads((attempt / "result.json").read_bytes())["status"]
            audit = attempt.parent / f"H4-{attempt.name}-audit.json"
            if audit.is_file():
                record["audit"] = audit.relative_to(ROOT).as_posix()
                record["audit_sha256"] = digest(audit)
        record["exit_code"] = code
        record["status"] = "PASS" if code == 0 else record.get("trial_status", "BLOCKED")
        if record["status"] == "PASS" and (code or "audit" not in record):
            record["status"] = "UNVERIFIED"
            code = 2
        record["exit_code"] = code
        return code
    except (Exception, KeyboardInterrupt) as error:
        record.update(status="BLOCKED", error=f"{type(error).__name__}: {error}", exit_code=2)
        raise
    finally:
        record["ended"] = now()
        save(status, record)
        print(record["status"] + ". Package launch record: " + str(status), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--verify", action="store_true", help="Verify the entire extracted package offline.")
    modes.add_argument("--test", action="store_true", help="Verify the package and run offline regression controls.")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise ValueError("Python 3.11 or later is required.")
    count = verify(ROOT)
    print(f"Verified {count} distribution files for {VERSION}.", flush=True)
    if args.test:
        offline_tests()
    elif not args.verify:
        return live(count)
    return 0


if __name__ == "__main__":
    try:
        sys.dont_write_bytecode = True
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f"BLOCKED. {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2)
