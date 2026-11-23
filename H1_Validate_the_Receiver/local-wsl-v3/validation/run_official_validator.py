"""Run the HL7 Java CLI locally; keep reports separate from limited structural checks."""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
import tarfile


DIRECTORY = Path(__file__).resolve().parent
FIXTURES = DIRECTORY.parent / "fixtures"
PROFILE = "http://hl7.org/fhir/uv/subscriptions-backport/StructureDefinition/backport-subscription-notification-r4"
STATUS = PROFILE.replace("-notification-r4", "-status-r4")
APPROVED_FIXTURES = {
    "admission-1.json": "9bcb21b61cd7718af7bd71972983ec88e759cddb12be19b5173f4c1f8c4f6ed8",
    "admission-2.json": "71e503096458a860e6d8741742d2b65b0e12874ce793e2b951385742038f2a94",
    "admission-3.json": "28e5c8f0cd5a09647aa465fab5575d24f8e4b34b78d9738126b4da04691726ca",
    "admission-4.json": "c11b68a91991b873ee9763b44b4726f73378ef19b30606c944486f3de8722a68",
}
NAMES = list(APPROVED_FIXTURES)
TERMINOLOGY = {
    "codesystem-subscription-notification-type.json": "f1c7b90d89998ae287391e888d1fc0933c10d9eb1c216204310d244c39c1fecf",
    "valueset-subscription-notification-type.json": "d628396a431f0cf68717ee9cac2ad1ccbc17614c0314e4465804332487089126",
}
VALIDATOR_RELEASE = "7.0.5"
VALIDATOR_SHA256 = "9dfe9f305b0f9389eceade2a4a1c9a2740d7b0e614066fdf07a78b571c76d92f"


def hash_file(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def package_identity(path):
    with tarfile.open(path, "r:gz") as package:
        manifest = json.load(package.extractfile("package/package.json"))
        if (manifest.get("name") != "hl7.fhir.uv.subscriptions-backport.r4"
                or manifest.get("version") != "1.1.0" or "4.0.0" not in manifest.get("fhirVersions", [])):
            raise ValueError("Expected the R4-specific 1.1.0 package. Its published metadata declares FHIR 4.0.0.")
        for canonical in (PROFILE, STATUS):
            slug = canonical.rsplit("/", 1)[-1]
            definition = json.load(package.extractfile(f"package/StructureDefinition-{slug}.json"))
            if definition.get("url") != canonical or definition.get("version") != "1.1.0":
                raise ValueError(f"The package does not contain the expected published profile {slug}.")
        return {"name": manifest["name"], "version": manifest["version"], "sha256": hash_file(path),
                "fhirVersions": manifest["fhirVersions"], "dependencies": manifest.get("dependencies", {})}


def verify_validator_jar(jar):
    digest = hash_file(jar)
    if digest != VALIDATOR_SHA256:
        raise ValueError(f"JAR differs from the publisher's SHA-256 for HL7 Validator {VALIDATOR_RELEASE}: {digest}")
    return digest


def verified_terminology(directory):
    files = []
    for filename, expected in TERMINOLOGY.items():
        path = directory / "terminology" / filename
        if hash_file(path) != expected:
            raise ValueError(f"Terminology file differs from the pinned HL7 publication: {filename}.")
        files.append(path)
    return files


def validation_arguments(jar, fixture, package, report, terminology):
    arguments = ["-jar", str(jar), str(fixture), "-version", "4.0.1", "-ig", str(package)]
    for resource in terminology:
        arguments.extend(["-ig", str(resource)])
    arguments.extend(["-profile", PROFILE, "-tx", "n/a"])
    if terminology:  # Original kits had neither the local terminology files nor the fictional URL option.
        arguments.extend(["-allow-example-urls", "true"])
    return [*arguments, "-output", str(report)]


def execute(args):
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    run = DIRECTORY / ("official-run-" + stamp)
    run.mkdir()
    jar, package = Path(args.jar).resolve(), Path(args.package).resolve()
    java = shutil.which(args.java)
    for description, available in (("Java", bool(java)), ("validator JAR", jar.is_file()), ("R4 package", package.is_file())):
        if not available:
            (run / "blocked.txt").write_text(f"{description} unavailable. No validator was executed.\n", encoding="utf-8")
            raise ValueError(f"{description} unavailable. Evidence: {run.name}/blocked.txt")
    terminology = verified_terminology(DIRECTORY)
    record = {"profile": PROFILE, "package_version": "1.1.0", "package": package_identity(package),
              "terminology": TERMINOLOGY,
              "validator_sha256": verify_validator_jar(jar), "validator_release": VALIDATOR_RELEASE,
              "version": "4.0.1", "started": stamp, "results": []}
    for filename, expected_hash in APPROVED_FIXTURES.items():
        fixture = FIXTURES / filename
        fixture_hash = hash_file(fixture)
        if fixture_hash != expected_hash:
            raise ValueError(f"Example URLs are allowed only for the approved H1 fixture: {filename}.")
        report = run / (fixture.stem + "-outcome.json")
        cmd = [java, *validation_arguments(jar, fixture, package, report, terminology)]
        result = {"filename": filename, "sha256": fixture_hash, "report": str(report.relative_to(DIRECTORY)),
                  "command": cmd}
        with (run / (fixture.stem + "-console.log")).open("wb") as console:
            proc = subprocess.run(cmd, stdout=console, stderr=subprocess.STDOUT, check=False)
        result["exit_code"] = proc.returncode
        result["report_sha256"] = hash_file(report) if report.is_file() else None
        record["results"].append(result)
        save(run / "invocations.json", record)
    print("Reports retained:", run.name)
    return run


def verify_recorded_command(row, run, uses_terminology):
    command = row.get("command")
    expected_length = 20 if uses_terminology else 14
    if (not isinstance(command, list) or len(command) != expected_length or
            not all(isinstance(arg, str) and arg.strip() for arg in command)):
        raise ValueError(f"Missing or unexpected validator command for {row['filename']}.")
    jar, fixture, package, report = command[2], command[3], command[7], command[-1]
    # Evidence can be reviewed on a different host. Check both Unix and Windows path spellings.
    name = row["filename"]
    report_parts = (run.name, Path(name).stem + "-outcome.json")
    if (PureWindowsPath(fixture).parts[-2:] != ("fixtures", name) or
            PureWindowsPath(report).parts[-3:] != ("validation", *report_parts) or
            PureWindowsPath(row["report"]).parts != report_parts):
        raise ValueError(f"Recorded fixture or report path differs from the H1 input: {name}.")
    terminology = []
    if uses_terminology:
        terminology = [command[9], command[11]]
        for path, filename in zip(terminology, TERMINOLOGY, strict=True):
            if PureWindowsPath(path).parts[-3:] != ("validation", "terminology", filename):
                raise ValueError(f"Recorded validator command has unexpected terminology: {name}.")
    if command[1:] != validation_arguments(jar, fixture, package, report, terminology):
        raise ValueError(f"Recorded validator command differs from the approved invocation: {name}.")


def finalize(run):
    run = run.resolve()
    if run.parent != DIRECTORY or not run.name.startswith("official-run-"):
        raise ValueError("Expected an official run directory beside this script.")
    record = json.loads((run / "invocations.json").read_text(encoding="utf-8"))
    if (record.get("profile") != PROFILE or record.get("package_version") != "1.1.0" or record.get("version") != "4.0.1" or
            record.get("validator_sha256") != VALIDATOR_SHA256 or record.get("validator_release") != VALIDATOR_RELEASE or
            [row["filename"] for row in record["results"]] != NAMES):
        raise ValueError("This run does not cover all four H1 files and the named release.")
    uses_terminology = "terminology" in record
    if uses_terminology and record["terminology"] != TERMINOLOGY:
        raise ValueError("Recorded terminology files differ from the pinned HL7 publications.")
    reviews = run / "warning-review.json"
    dispositions = json.loads(reviews.read_text(encoding="utf-8")) if reviews.is_file() else {}
    warnings = []
    for row in record["results"]:
        verify_recorded_command(row, run, uses_terminology)
        report = (DIRECTORY.joinpath(*PureWindowsPath(row["report"]).parts)).resolve()
        if report.parent != run or not report.exists() or hash_file(report) != row["report_sha256"]:
            raise ValueError(f"Missing or changed validator report for {row['filename']}.")
        if (row["exit_code"] != 0 or row["sha256"] != APPROVED_FIXTURES[row["filename"]] or
                hash_file(FIXTURES / row["filename"]) != row["sha256"]):
            raise ValueError(f"Failed validation process or changed fixture: {row['filename']}.")
        outcome = json.loads(report.read_text(encoding="utf-8-sig"))
        issues = outcome.get("issue")
        if outcome.get("resourceType") != "OperationOutcome" or not isinstance(issues, list) or not issues:
            raise ValueError(f"Invalid or empty OperationOutcome for {row['filename']}.")
        for index, issue in enumerate(issues):
            severity = issue.get("severity")
            if severity not in ("information", "warning", "success"):
                raise ValueError(f"Issue {index} for {row['filename']} has severity {severity}.")
            detail = json.dumps(issue).lower()
            concerning = any(word in detail for word in ("unknown", "not check", "unable", "unresolved", "could not", "not found", "not validat", "not support"))
            if severity == "warning" or concerning:
                key = f"{row['report_sha256']}:{index}"
                disposition = dispositions.get(key, {})
                reason = disposition.get("reason")
                if disposition.get("conformance_checked") is not True or not isinstance(reason, str) or not reason.strip():
                    warnings.append({"key": key, "filename": row["filename"], "issue": issue})
        row["passed"] = True
    if warnings:
        save(run / "issues-requiring-review.json", warnings)
        raise ValueError("Inspect the flagged diagnostics before finalizing. Full conformance checks must be resolved locally.")
    if reviews.is_file():
        record["diagnostic_review"] = str(reviews.relative_to(DIRECTORY))
    manifest = DIRECTORY / "official_validation.json"
    if manifest.exists():
        archive = run / "previous_official_validation.json"
        if not archive.exists():
            shutil.copy2(manifest, archive)
    pending = run / "pending-manifest.json"
    save(pending, record)
    pending.replace(manifest)
    print("Passed all four official local runs. Manifest:", manifest.name)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--java", default="java")
    cli.add_argument("--jar", default=str(DIRECTORY / "validator_cli.jar"))
    cli.add_argument("--package", default=str(DIRECTORY / "package.r4.tgz"))
    cli.add_argument("--finalize", type=Path, help="Re-check an existing run after review of warnings.")
    args = cli.parse_args()
    try:
        finalize(args.finalize if args.finalize else execute(args))
    except (ValueError, OSError, KeyError, tarfile.TarError) as error:
        print(error, file=sys.stderr)
        sys.exit(2)
