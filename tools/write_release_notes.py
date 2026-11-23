"""Fill the release notes from a hosted run's own files, never by hand.

Usage: python3 -I tools/write_release_notes.py TEMPLATE ENVIRONMENT_TXT AUDIT_JSON ARCHIVE_ZIP RECORD_ZIP RUN_ID RUN_URL OUTPUT
Every value in braces in TEMPLATE comes from those files: the date and host lines from
environment.txt, the counts from the audit file, each asset name and SHA-256 from the file itself.
Stops with an error unless the run exited 0, 45 trials passed with nothing flagged, not run or
global, the runner and the second program agree on every trial, and no brace is left unfilled.
"""
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

template, env_path, audit_path, archive, record, run_id, run_url, output = sys.argv[1:9]

env = {}
for line in Path(env_path).read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        env.setdefault(key, value.strip().strip('"'))
    elif line.startswith("Python "):
        env["python"] = line.strip()
    elif line.startswith("Docker Compose version "):
        env["compose"] = "Docker Compose " + line.split("Docker Compose version ", 1)[1].strip()

if env.get("live_exit") != "0":
    raise SystemExit(f"Live run exit is {env.get('live_exit')!r}, not 0.")

audit = json.loads(Path(audit_path).read_text(encoding="utf-8"))
counts = audit["counts"]
trials = audit["trials"]
agree = sum(1 for t in trials.values() if t["runner"] == t["independent"])
passed = counts.get("PASS", 0)
if (passed, len(audit["flagged"]), len(audit["not_run"]), len(audit["global_findings"]), agree, len(trials)) != (45, 0, 0, 0, 45, 45):
    raise SystemExit(f"Audit is not 45 PASS with full agreement: {counts}, agree {agree} of {len(trials)}.")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


day = datetime.date.fromisoformat(env["recorded_utc"][:10])
values = {
    "RUN_DATE": f"{day:%b} {day.day}, {day.year}",
    "RUN_ID": run_id,
    "RUN_URL": run_url,
    "OS_NAME": env["PRETTY_NAME"],
    "RUNNER_IMAGE": env["runner_image"],
    "PYTHON_VERSION": env["python"],
    "DOCKER_ENGINE": env["docker_engine"],
    "COMPOSE_VERSION": env["compose"],
    "PASS_COUNT": str(passed),
    "FLAGGED_COUNT": str(len(audit["flagged"])),
    "NOT_RUN_COUNT": str(len(audit["not_run"])),
    "GLOBAL_COUNT": str(len(audit["global_findings"])),
    "AGREE_COUNT": str(agree),
    "ARCHIVE_NAME": Path(archive).name,
    "ARCHIVE_SHA256": sha(archive),
    "AUDIT_NAME": Path(audit_path).name,
    "AUDIT_SHA256": sha(audit_path),
    "RECORD_NAME": Path(record).name,
    "RECORD_SHA256": sha(record),
}
text = Path(template).read_text(encoding="utf-8")
used = set(re.findall(r"\{([A-Z0-9_]+)\}", text))
missing = used - values.keys()
if missing:
    raise SystemExit(f"No value for {sorted(missing)}.")
for key in used:
    text = text.replace("{" + key + "}", values[key])
if re.search(r"\{[A-Z0-9_]+\}", text):
    raise SystemExit("A brace is left unfilled.")
Path(output).write_text(text, encoding="utf-8")
print(text)
