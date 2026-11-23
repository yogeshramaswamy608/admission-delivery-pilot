"""Stage the official H1 validator and warm a private package cache on the test host."""

import argparse
from datetime import datetime, timezone
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


JAR = "https://github.com/hapifhir/org.hl7.fhir.core/releases/download/7.0.5/validator_cli.jar"
GUIDE = "https://hl7.org/fhir/uv/subscriptions-backport/STU1.1/package.r4.tgz"


def private_java_env(home: Path):
    """Build the child environment without altering the caller or contacting a service."""
    location = str(home.absolute())
    if any(character in location for character in ('"', "\n", "\r")):
        raise ValueError("Use a Java home path without quotes or line breaks.")
    env = dict(os.environ)
    for variable in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"):
        if "-Duser.home=" in env.get(variable, ""):
            raise ValueError(f"Remove the inherited user.home override from {variable} for this run.")
        # A portable run should not inherit personal proxy credentials or alternate JVM agents.
        env.pop(variable, None)
    for variable in ("FHIR_SETTINGS_PATH", "FHIR_PACKAGE_CACHE", "FHIR_PACKAGE_CACHE_MODE"):
        env.pop(variable, None)
    env.update(HOME=location, USERPROFILE=location, JAVA_TOOL_OPTIONS=f'"-Duser.home={location}"')
    return env


def validator_helpers():
    # The unpacked kit and the project source tree have different root locations.
    directory = Path(__file__).resolve().parent
    for root in (directory, directory.parent):
        path = root / "validation" / "run_official_validator.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("h1_profile_checks", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise ValueError("The kit is missing validation/run_official_validator.py.")


class HttpsRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise ValueError("Refused a dependency redirect to a non-HTTPS location.")
        return super().redirect_request(request, file, code, message, headers, newurl)


def stage_file(target, url, verify):
    if target.is_symlink():
        raise ValueError(f"Expected a regular file at {target}.")
    if target.exists():
        return verify(target)
    # Verify before publishing; an overlapping attempt may win, but is never overwritten.
    with tempfile.TemporaryDirectory(dir=target.parent) as staging:
        partial = Path(staging) / target.name
        opener = build_opener(HttpsRedirects())
        request = Request(url, headers={"User-Agent": "H1-validator-staging/1"})
        with opener.open(request, timeout=120) as response, partial.open("xb") as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
        identity = verify(partial)
        try:
            if os.name == "nt":
                os.rename(partial, target)  # Windows refuses to replace an existing destination.
            else:
                os.link(partial, target)
        except FileExistsError:
            return verify(target)
        return identity


def save_json(path, value):
    with path.open("x", encoding="utf-8") as destination:
        json.dump(value, destination, indent=2)
        destination.write("\n")


def warm_cache(java, tools, attempt):
    home = tools / "java-home"
    if home.is_symlink() or (home / ".fhir").is_symlink() or (home / ".fhir" / "packages").is_symlink():
        raise ValueError("The Java home and package cache must be private directories on this test host.")
    home.mkdir(exist_ok=True)
    example = attempt / "cache-probe.json"
    # Generic public content lets the validator resolve the IG and its own dependencies first.
    save_json(example, {"resourceType": "OperationOutcome", "id": "package-cache-probe",
                        "text": {"status": "generated", "div": '<div xmlns="http://www.w3.org/1999/xhtml">Cache preparation.</div>'},
                        "issue": [{"severity": "information", "code": "informational", "diagnostics": "Cache preparation."}]})
    output = attempt / "cache-probe-outcome.json"
    command = [java, "-jar", str(tools / "validator_cli.jar"), str(example), "-version", "4.0.1",
               "-ig", str(tools / "package.r4.tgz"), "-tx", "n/a", "-output", str(output)]
    with (attempt / "cache-probe.log").open("xb") as log:
        result = subprocess.run(command, cwd=attempt, env=private_java_env(home), stdout=log, stderr=subprocess.STDOUT)
    if result.returncode or not output.is_file():
        raise ValueError("Java could not finish the generic cache probe. See cache-probe.log.")
    outcome = json.loads(output.read_text(encoding="utf-8-sig"))
    if outcome.get("resourceType") != "OperationOutcome" or any(
        issue.get("severity") in ("error", "fatal") for issue in outcome.get("issue", [])
    ):
        raise ValueError("The generic probe has errors. Inspect its report before starting H1.")
    cache = home / ".fhir" / "packages"
    installed = []
    for manifest in sorted(cache.glob("*/package/package.json")):
        package = json.loads(manifest.read_text(encoding="utf-8-sig"))
        installed.append(f"{package['name']}#{package['version']}")
    if "hl7.fhir.r4.core#4.0.1" not in installed:
        raise ValueError("The private cache does not contain FHIR R4 core 4.0.1. Inspect cache-probe.log.")
    return installed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-dir", type=Path, default=Path("tools"))
    parser.add_argument("--report-dir", type=Path, help="Create this fresh directory for the staging receipt and cache-probe log, including on a failed attempt.")
    parser.add_argument("--java", default="java")
    parser.add_argument("--dry-run", action="store_true", help="Show the public downloads and cache location without changing files or using the network.")
    args = parser.parse_args()
    tools = args.tools_dir.resolve()
    print(f"Official validator: {JAR}\nR4 guide: {GUIDE}\nPrivate Java home: {tools / 'java-home'}", flush=True)
    if args.dry_run:
        return 0
    try:
        if args.tools_dir.is_symlink():
            raise ValueError("The tools directory must be a local directory on the private test host.")
        tools.mkdir(parents=True, exist_ok=True)
        if args.report_dir:
            attempt = args.report_dir.absolute()
            attempt.mkdir(parents=True, exist_ok=False)
        else:
            attempt = Path(tempfile.mkdtemp(prefix="stage-", dir=tools))
    except (OSError, ValueError) as error:
        print(f"BLOCKED: Could not create fresh staging directories: {error}", file=sys.stderr)
        return 2
    print(f"Staging evidence: {attempt}", flush=True)
    record = {"started_utc": datetime.now(timezone.utc).isoformat(), "jar_url": JAR, "guide_url": GUIDE}
    try:
        java = shutil.which(args.java)
        if not java:
            raise ValueError("Java is unavailable on this host. Install the test host's Java runtime first.")
        checks = validator_helpers()
        record["jar_sha256"] = stage_file(tools / "validator_cli.jar", JAR, checks.verify_validator_jar)
        record["guide"] = stage_file(tools / "package.r4.tgz", GUIDE, checks.package_identity)
        record["cached_packages"] = warm_cache(java, tools, attempt)
        record["status"] = "STAGED"
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, http.client.HTTPException, subprocess.SubprocessError) as error:
        record.update(status="BLOCKED", reason=str(error))
    record["ended_utc"] = datetime.now(timezone.utc).isoformat()
    save_json(attempt / "stage-result.json", record)
    print(record["status"], record.get("reason", "Dependency cache prepared. H1 notices have not been validated."))
    return 0 if record["status"] == "STAGED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
