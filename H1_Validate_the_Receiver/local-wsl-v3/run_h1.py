"""Run the H1 receiver test from a portable kit and keep evidence for every attempt."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


HERE = Path(__file__).resolve().parent
SOURCE = HERE
if not (HERE / "receiver_check.py").is_file() and HERE.name == "portable" and HERE.parent.name == "H1_Validate_the_Receiver":
    SOURCE = HERE.parent
validator_spec = importlib.util.spec_from_file_location("h1_validator", SOURCE / "validation" / "run_official_validator.py")
validator = importlib.util.module_from_spec(validator_spec)
validator_spec.loader.exec_module(validator)
PLAN = [
    "Validate the four exact fictional notices against the HL7 R4 Backport 1.1.0 profile.",
    "Before sending any notice, look up all four admissions and require four HTTP 204 responses.",
    "Send each notice once and require four HTTP 200 responses.",
    "Read admission three and require exactly one row containing the complete matching Encounter.",
    "Resend the exact third notice with a new request ID and require a duplicate-specific HTTP 422.",
    "Read admission three again and require exactly one complete matching row.",
]
SCRIPTS = ("receiver_check.py", "validation/run_official_validator.py")
PUBLISHER_IMAGE = "drajerhealth/ecr-now@sha256:28a195c6138316988689adf21afcc2a239bf1f95dc9bc0b52e87fb113adc2976"
TESTER_IMAGE = "python@sha256:e8c69d86735f6df3efa06b324694fe51f0422785f83a3bd463052636c2421944"


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def inputs():
    files = [Path(name) for name in SCRIPTS]
    for name, expected in validator.APPROVED_FIXTURES.items():
        relative = Path("fixtures") / name
        require(digest(SOURCE / relative) == expected, f"{name} differs from the approved synthetic input.")
        files.append(relative)
    files.extend(path.relative_to(SOURCE) for path in validator.verified_terminology(SOURCE / "validation"))
    for relative in files:
        require((SOURCE / relative).is_file(), f"Missing kit file: {relative}.")
    return files


def invoke(script, args, log, env=None):
    with log.open("xb") as output:
        process = subprocess.run(
            [sys.executable, "-B", str(script), *map(str, args)],
            stdout=output, stderr=subprocess.STDOUT, check=False, env=env,
        )
    return process.returncode


def prepare(attempt, files):
    work = attempt / "work"
    for name in files:
        destination = work / name
        destination.parent.mkdir(exist_ok=True, parents=True)
        shutil.copyfile(SOURCE / name, destination)
    write_json(attempt / "expected.json", {"scope": "H1 only", "expected": PLAN})
    write_json(attempt / "kit-identity.json", {
        "wrapper_sha256": digest(Path(__file__)),
        "files": {name.as_posix(): digest(work / name) for name in files},
        "python_version": sys.version.split()[0],
    })
    return work


def offline_review(previous, disposition, work):
    require(previous.is_dir() and previous.name.startswith("official-run-"), "Select the original official-run directory.")
    require((previous / "issues-requiring-review.json").is_file(), "Original run has no issues-requiring-review.json.")
    require(disposition and disposition.is_file(), "Supply the documented warning dispositions with --warning-review.")
    original = read_json(previous / "invocations.json")
    require([row["filename"] for row in original["results"]] == [f"admission-{n}.json" for n in range(1, 5)],
            "Original run did not cover the four H1 notices.")
    dest = work / "validation" / previous.name
    dest.mkdir()
    for name in ["invocations.json", "issues-requiring-review.json"]:
        shutil.copyfile(previous / name, dest / name)
    for n in range(1, 5):
        for ending in ["-outcome.json", "-console.log"]:
            name = f"admission-{n}{ending}"
            shutil.copyfile(previous / name, dest / name)
    shutil.copyfile(disposition, dest / "warning-review.json")
    return dest


def engine_environment():
    return {key: value for key, value in os.environ.items()
            if key not in ("DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_API_VERSION")}


def local_engine():
    binary = shutil.which("docker")
    require(binary, "A local Docker engine is required for offline profile validation.")
    env = engine_environment()
    context = os.environ.get("DOCKER_CONTEXT")
    endpoint = os.environ.get("DOCKER_HOST")
    if context or not endpoint:
        if not context:
            context = subprocess.check_output([binary, "context", "show"], text=True, env=env).strip()
        endpoint = subprocess.check_output(
            [binary, "context", "inspect", "--format", '{{(index .Endpoints "docker").Host}}', context],
            text=True, env=env,
        ).strip()
    require(endpoint.startswith("unix:///"), "Profile validation requires a Docker socket on this private Linux computer.")
    return [binary, "--host", endpoint]


def make_offline_java(args, work, attempt):
    require(sys.platform.startswith("linux"), "The included network-isolated validator requires Linux with a local Docker engine.")
    home = (args.java_home or args.jar.resolve().parent / "java-home").resolve()
    require(home.is_dir(), "Stage the official validator's Java home and FHIR cache before testing.")
    docker = local_engine()
    env = engine_environment()
    image_id = subprocess.check_output(
        [*docker, "image", "inspect", "--format", "{{.Id}}", PUBLISHER_IMAGE], text=True, env=env,
    ).strip()
    require(re.fullmatch("sha256:[0-9a-f]{64}", image_id), "Pinned receiver image must already exist on this local Docker engine.")
    config = attempt / "offline-java.json"
    write_json(config, {
        "docker": docker, "image": PUBLISHER_IMAGE, "image_id": image_id, "network": "none",
        "java_home": str(home), "work": str(work.resolve()), "jar": str(args.jar.resolve()),
        "package": str(args.package.resolve()),
    })
    # The existing validator accepts a Java executable. Its adapter translates only the H1 invocation.
    shim = attempt / "offline-java"
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--container-java", str(config.resolve())]
    shim.write_text("#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n', encoding="utf-8")
    shim.chmod(0o700)
    return str(shim.resolve())


def bind_mount(source, destination, readonly=True):
    require(source.exists() and "," not in str(source), "Docker needs an existing mount path without a comma.")
    option = ",readonly" if readonly else ""
    return ["--mount", f"type=bind,src={source},dst={destination}{option}"]


def offline_java_command(config, arguments):
    work = Path(config["work"])
    jar, package = Path(config["jar"]), Path(config["package"])
    require(len(arguments) >= 4, "Unexpected validator invocation.")
    fixture, report = Path(arguments[2]).resolve(), Path(arguments[-1]).resolve()
    require(fixture.parent == work / "fixtures" and fixture.name in validator.APPROVED_FIXTURES,
            "The validator adapter only accepts the four H1 fixtures.")
    require(digest(fixture) == validator.APPROVED_FIXTURES[fixture.name],
            "The validator adapter requires the approved synthetic fixture contents.")
    require(report.parent.parent == work / "validation" and report.parent.name.startswith("official-run-")
            and report.name == fixture.stem + "-outcome.json", "Invalid validation report destination.")
    terminology = validator.verified_terminology(work / "validation")
    require(arguments == validator.validation_arguments(jar, fixture, package, report, terminology),
            "The validator adapter only accepts the approved H1 profile validation flags.")
    image = config["image"]
    require(image == PUBLISHER_IMAGE and config["network"] == "none", "Offline Java configuration does not match the pinned runtime.")
    docker = config["docker"]
    require(isinstance(docker, list) and len(docker) == 3 and docker[0] == shutil.which("docker")
            and docker[1] == "--host" and docker[2].startswith("unix:///"),
            "Offline profile validation requires the local Docker engine.")
    command = [*docker, "run", "--rm", "--pull=never", "--network", "none", "--platform", "linux/amd64",
               "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp",
               "--user", f"{os.getuid()}:{os.getgid()}", "--env", "HOME=/h1/home", "--workdir", "/tmp"]
    for source, target, readonly in [
        (jar, "/h1/validator.jar", True), (package, "/h1/package.r4.tgz", True),
        (fixture, "/h1/" + fixture.name, True), (Path(config["java_home"]), "/h1/home", False),
        (report.parent, "/h1/reports", False),
    ]:
        command.extend(bind_mount(source, target, readonly))
    container_terminology = []
    for resource in terminology:
        destination = "/h1/terminology/" + resource.name
        command.extend(bind_mount(resource, destination))
        container_terminology.append(destination)
    arguments = validator.validation_arguments("/h1/validator.jar", "/h1/" + fixture.name, "/h1/package.r4.tgz",
                                               "/h1/reports/" + report.name, container_terminology)
    return [*command, "--entrypoint", "java", image, "-Duser.home=/h1/home", *arguments]


def container_java(config_file, arguments):
    command = offline_java_command(read_json(Path(config_file)), arguments)
    print("Actual offline validator command:", shlex.join(command), flush=True)
    return subprocess.run(command, env=engine_environment(), check=False).returncode


def official_validation(args, work, attempt):
    if args.review_run:
        copied = offline_review(args.review_run, args.warning_review, work)
        parameters = ["--finalize", copied]
    else:
        require(not args.warning_review, "--warning-review also requires --review-run.")
        java = make_offline_java(args, work, attempt)
        parameters = ["--java", java, "--jar", args.jar.resolve(), "--package", args.package.resolve()]
    return invoke(work / SCRIPTS[1], parameters, attempt / "validator.log")


def describe_validation_stop(work, result):
    reports = sorted((work / "validation").glob("official-run-*/admission-*-outcome.json"))
    result["official_reports_saved"] = len(reports)
    for report in reports:
        outcome = read_json(report)
        if outcome.get("resourceType") == "OperationOutcome" and any(
            issue.get("severity") in ("error", "fatal") for issue in outcome.get("issue", [])
        ):
            result.update(verdict="STOPPED", profile_validation="FAIL")
            return "An official report contains an error or fatal issue. Inspect the raw reports before rerunning."
    if list((work / "validation").glob("official-run-*/issues-requiring-review.json")):
        return "The official validator flagged diagnostics for review. Resolve each flagged issue before any receiver test."
    return "Validation did not complete successfully. Inspect validator.log and work/validation/."


def verify_build_record(path, receiver_url):
    data = read_json(path)
    require(data.get("receiver_url", "").rstrip("/") == receiver_url.rstrip("/"),
            "Deployment receipt URL differs from the requested receiver.")
    require(receiver_url == "http://127.0.0.1:8081", "Expected the receiver's private container loopback address.")
    engine_id = re.compile("[0-9a-f]{64}")
    image_id = re.compile("sha256:[0-9a-f]{64}")
    receiver, database, network, transport = data["receiver"], data["database"], data["network"], data["transport"]
    for name, container in [("receiver", receiver), ("database", database)]:
        require(container.get("running") is True and engine_id.fullmatch(container.get("container_id", "")),
                f"Missing running {name} container identity.")
        require(image_id.fullmatch(container.get("image_id", "")), f"Invalid {name} image identity.")
    require(PUBLISHER_IMAGE in receiver["repo_digests"], "Running receiver image does not identify the pinned publisher digest.")
    require(receiver["container_id"] != database["container_id"], "Receiver and database container identities must differ.")
    require(database["database_name"] and database["server_version"] and database["repo_digests"],
            "Database receipt is missing its database, installed version or repository digest.")
    require(network.get("internal") is True and network.get("name") and engine_id.fullmatch(network.get("id", "")),
            "Missing the observed private Docker network identity.")
    port_map = receiver["ports"]
    require(isinstance(port_map, dict) and not any(port_map.values()), "The test receiver must have no host-published ports.")
    require(transport.get("mode") == "container-loopback" and transport.get("tester_image") == TESTER_IMAGE
            and transport.get("receiver_container_id") == receiver["container_id"]
            and str(transport.get("local_engine", "")).startswith("unix:///"),
            "The checker must share the running receiver's private loopback network.")
    return {
        "receiver_url": data["receiver_url"],
        "receiver": {key: receiver[key] for key in ("running", "container_id", "image_id", "repo_digests", "ports")},
        "database": {key: database[key] for key in ("running", "container_id", "image_id", "repo_digests", "database_name", "server_version")},
        "network": {key: network[key] for key in ("name", "id", "internal")},
        "transport": {key: transport[key] for key in ("mode", "tester_image", "receiver_container_id", "local_engine")},
    }


def verified_receiver_command(receipt, work):
    docker = local_engine()
    require(docker[-1] == receipt["transport"]["local_engine"], "Current local Docker engine differs from its deployment receipt.")
    env = engine_environment()
    receiver_id = receipt["receiver"]["container_id"]
    status = subprocess.check_output([
        *docker, "container", "inspect", "--format",
        "{{.State.Running}}|{{.Image}}|{{json .NetworkSettings.Networks}}|{{json .NetworkSettings.Ports}}", receiver_id,
    ], text=True, env=env).strip().split("|", 3)
    require(len(status) == 4 and status[0] == "true" and status[1] == receipt["receiver"]["image_id"],
            "The observed running receiver identity changed.")
    networks = json.loads(status[2])
    require(list(networks) == [receipt["network"]["name"]]
            and networks[receipt["network"]["name"]].get("NetworkID") == receipt["network"]["id"],
            "The receiver is not on its single recorded internal network.")
    require(not any((json.loads(status[3]) or {}).values()), "The receiver now has a host-published port.")
    internal = subprocess.check_output([*docker, "network", "inspect", "--format", "{{.Internal}}", receipt["network"]["id"]],
                                       text=True, env=env).strip()
    require(internal == "true", "The current receiver network is not internal.")
    return [*docker, "run", "--rm", "--pull=never", "--platform", "linux/amd64", "--network", "container:" + receiver_id,
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp",
            "--user", f"{os.getuid()}:{os.getgid()}", *bind_mount(work, "/work"),
            *bind_mount(work / "runs", "/work/runs", False), "--workdir", "/work", TESTER_IMAGE,
            "python", "-B", "/work/receiver_check.py", "--receiver", receipt["receiver_url"],
            "--build-record", "/work/deployment-record.json"]


def execute(args, attempt, result):
    work = prepare(attempt, inputs())
    result["stage"] = "official profile validation"
    code = official_validation(args, work, attempt)
    if code:
        raise ValueError(describe_validation_stop(work, result))
    require((work / "validation" / "official_validation.json").is_file(), "Validator returned without its verified manifest.")
    result["profile_validation"] = "PASS"
    result["official_reports_saved"] = 4
    if args.validate_only:
        result.update(stage="profile validation only", verdict="VALIDATION ONLY", detail="Four verified profiles. Receiver not tested.")
        return 0
    result["stage"] = "local receiver prerequisites"
    origin = urlsplit(args.receiver or "")
    require(origin.hostname in ("127.0.0.1", "::1") and origin.scheme in ("http", "https")
            and not (origin.username or origin.password or origin.query or origin.fragment),
            "Supply the numeric loopback URL of the isolated H1 receiver.")
    require(args.build_record and args.build_record.is_file(), "Supply the actual running receiver's deployment receipt.")
    receipt = verify_build_record(args.build_record, args.receiver)
    write_json(work / "deployment-record.json", receipt)
    (work / "runs").mkdir()
    command = verified_receiver_command(receipt, work.resolve())
    result["stage"] = "live receiver"
    with (attempt / "receiver.log").open("x", encoding="utf-8") as log:
        print("Actual checker command:", shlex.join(command), file=log, flush=True)
        code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False, env=engine_environment()).returncode
    outcomes = list((work / "runs").glob("*/result.json"))
    require(len(outcomes) == 1, "Missing the receiver check's raw result. Inspect receiver.log.")
    observed = read_json(outcomes[0])
    result["receiver_result_file"] = outcomes[0].relative_to(attempt).as_posix()
    result["http_responses_saved"] = len(list(outcomes[0].parent.glob("*.response")))
    result["http_requests_started"] = len(list(outcomes[0].parent.glob("0*.json"))) - len(list(outcomes[0].parent.glob("*.request.json")))
    if code or observed["result"] != "PASS":
        result["verdict"] = "STOPPED" if result["http_responses_saved"] else "BLOCKED"
        result["detail"] = observed.get("reason", "The receiver check did not finish successfully.")
        return 2
    require(result["http_responses_saved"] == result["http_requests_started"] == 11,
            "Expected eleven recorded receiver responses for a passing live check.")
    result.update(verdict="PASS", detail="Four profiles and all eleven live HTTP exchanges passed. Acceptance remains with Yogesh.")
    return 0


def write_inventory(attempt):
    files = sorted(p for p in attempt.rglob("*") if p.is_file())
    (attempt / "SHA256SUMS.txt").write_text("".join(
        f"{digest(path)}  {path.relative_to(attempt).as_posix()}\n" for path in files
    ), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Verify the kit inputs and preview the test without connecting.")
    mode.add_argument("--validate-only", action="store_true", help="Run only the official checks on the four fixtures.")
    parser.add_argument("--receiver", help="Numeric loopback URL of the isolated H1 receiver.")
    parser.add_argument("--build-record", type=Path, help="Sanitized JSON receipt from the local receiver launcher.")
    parser.add_argument("--evidence-dir", type=Path, default=HERE / "evidence")
    parser.add_argument("--offline-docker", action="store_true", help="Use the pinned image's Java with networking disabled (the default).")
    parser.add_argument("--java-home", type=Path, help="Private Java user.home with staged FHIR packages.")
    parser.add_argument("--jar", type=Path, default=HERE / "validation" / "validator_cli.jar")
    parser.add_argument("--package", type=Path, default=HERE / "validation" / "package.r4.tgz")
    parser.add_argument("--review-run", type=Path, help="Original official-run directory with diagnostics requiring review.")
    parser.add_argument("--warning-review", type=Path, help="Separate disposition JSON backed by actual resolution evidence.")
    args = parser.parse_args()
    require(sys.version_info >= (3, 11), "Python 3.11 or newer is required.")
    if args.dry_run:
        inputs()
        process = subprocess.run([sys.executable, "-B", str(SOURCE / SCRIPTS[0]), "--dry-run"], check=False)
        require(process.returncode == 0, "Receiver checker dry run failed.")
        print("\n".join(f"{n}. {item}" for n, item in enumerate(PLAN, start=1)))
        print("DRY RUN: input byte hashes verified; no profile validation, HTTP requests, or database writes.")
        return 0
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    attempt = args.evidence_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex)
    attempt.mkdir()
    result = {"started_utc": utc_now(), "scope": "H1 only", "verdict": "BLOCKED", "stage": "kit inputs",
              "profile_validation": "NOT VERIFIED", "official_reports_saved": 0, "http_requests_started": 0, "http_responses_saved": 0}
    status = 2
    try:
        status = execute(args, attempt, result)
    except (ValueError, OSError, KeyError, TypeError, IndexError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        result["detail"] = str(error) or "Interrupted. Retain the partial evidence."
    finally:
        result["ended_utc"] = utc_now()
        write_json(attempt / "result.json", result)
        write_inventory(attempt)
        print(f"{result['verdict']}: {result['detail']}\nEvidence directory: {attempt.resolve()}")
    return status


if __name__ == "__main__":
    try:
        if sys.argv[1:2] == ["--container-java"]:
            raise SystemExit(container_java(sys.argv[2], sys.argv[3:]))
        raise SystemExit(main())
    except (ValueError, KeyError, IndexError, TypeError, OSError, subprocess.SubprocessError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        raise SystemExit(2)
