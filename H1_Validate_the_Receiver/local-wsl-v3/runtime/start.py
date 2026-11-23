"""Provision an isolated eCRNow receiver and record its observed identity and readiness."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time
import uuid


HERE = Path(__file__).resolve().parent
SOURCE = "89da654471b651dd70592f371e577fffa3c5f04e"
VENDOR_SQL = "/java-app/src/main/resources/db-scripts/4.xx_db_migration_scripts/psql_db_scripts"
KEY_COLUMNS = "fhir_server_base_url,patient_id,notification_resource_id,notification_resource_type,trigger_event"
TESTER = "python@sha256:e8c69d86735f6df3efa06b324694fe51f0422785f83a3bd463052636c2421944"
READINESS_PATH = "/api/notificationContexts?patientId=drill-ready-check&notificationResourceId=drill-ready-check"
SOCKET_PROBE = """
import http.client, json, sys
c = http.client.HTTPConnection("127.0.0.1", 8081, timeout=10)
try:
    c.request("GET", sys.argv[1])
    r = c.getresponse()
    print(json.dumps({"method": "GET", "path": sys.argv[1], "status": r.status, "body_bytes": len(r.read())}))
finally:
    c.close()
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def git_blob_id(contents):
    header = b"blob " + str(len(contents)).encode("ascii") + b"\x00"
    return hashlib.sha1(header + contents).hexdigest()


def invoke(arguments, *, data=None, timeout=60, check=True):
    result = subprocess.run(arguments, input=data, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"Docker exited {result.returncode}. {safe_diagnostics(result.stderr)}")
    return result


def safe_diagnostics(raw):
    # Return only known, context-free error fragments and Java exception types.
    text = raw.decode(errors="replace")
    markers = (
        "H1_CONFIG_PERMISSION", "no space left on device", "permission denied", "access denied",
        "manifest unknown", "no such image", "no matching manifest", "pull access denied",
        "unauthorized", "cannot connect", "connection refused", "network is unreachable",
        "no such host", "i/o timeout", "TLS handshake timeout", "exec format error",
        "mount denied", "operation not permitted", "address already in use", "out of memory",
        "failed to mount", "invalid mount config", "connection timed out", "no such file",
    )
    found = [marker for marker in markers if marker.lower() in text.lower()]
    found.extend(sorted(set(re.findall(r"\b(?:[A-Za-z]+\.)+[A-Z]\w*(?:Exception|Error)\b", text)))[:6])
    return ", ".join(found) or "Inspect the retained project on the test host for detailed diagnostics."


def local_engine():
    require(shutil.which("docker"), "Docker is unavailable on this computer.")
    endpoint = os.environ.get("DOCKER_HOST")
    if os.environ.get("DOCKER_CONTEXT") or not endpoint:
        context = invoke(["docker", "context", "show"]).stdout.decode().strip()
        endpoint = invoke([
            "docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}", context
        ]).stdout.decode().strip()
    require(endpoint.startswith(("unix:///", "npipe:////./pipe/")), "Select a local Docker engine.")
    arguments = ["docker", "--host", endpoint]
    require(invoke(arguments + ["info", "--format", "{{.OSType}}"]).stdout.strip() == b"linux",
            "The Docker engine must support Linux containers.")
    invoke(arguments + ["compose", "version"])
    return arguments


class Trial:
    def __init__(self, output):
        self.output = output
        self.project = "h1-" + uuid.uuid4().hex
        self.stage = "preflight"
        self.docker = []
        self.compose = []
        self.diagnostic_container = None

    def stage_started(self, stage):
        self.stage = stage
        item = {"utc": now(), "stage": stage, "project": self.project}
        with (self.output / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item) + "\n")
        print(stage, flush=True)

    def inspect(self, kind, identity):
        return json.loads(invoke(self.docker + [kind, "inspect", identity]).stdout)[0]

    def container_id(self, service):
        identity = invoke(self.compose + ["ps", "--all", "--quiet", service]).stdout.decode().strip()
        require(identity and "\n" not in identity, f"Expected one container for {service}.")
        return self.inspect("container", identity)["Id"]

    def state(self, container, pinned, network):
        state = self.inspect("container", container)
        image = self.inspect("image", pinned)
        require(image["Os"] == "linux" and image["Architecture"] == "amd64", "Image platform mismatch.")
        require(state["Image"] == image["Id"], "Container is running a different image.")
        digests = image.get("RepoDigests") or []
        require(any(d.split("@")[-1] == pinned.split("@")[-1] for d in digests),
                "Pinned image digest absent from observed Docker metadata.")
        networks = state["NetworkSettings"]["Networks"]
        require(set(networks) == {network["Name"]} and networks[network["Name"]]["NetworkID"] == network["Id"],
                "Container is attached outside the new private network.")
        require(state["State"]["Running"], "Container stopped before readiness.")
        return {"container_id": state["Id"], "image_id": state["Image"], "repo_digests": digests,
                "running": True, "ports": state["NetworkSettings"]["Ports"] or {}}

    def query(self, database, query):
        command = ["exec", database, "psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-U", "h1",
                   "-d", "ecrnow", "-c", query]
        return invoke(self.docker + command).stdout.decode().strip()

    def migrate(self, receiver, database):
        copy = invoke(self.docker + ["cp", f"{receiver}:{VENDOR_SQL}/.", "-"])
        manifest = json.loads((HERE / "migrations.json").read_bytes())
        observed = []
        with tarfile.open(fileobj=io.BytesIO(copy.stdout), mode="r:*") as archive:
            files = {}
            for info in archive:
                if info.isfile():
                    name = info.name.removeprefix("./")
                    require(name not in files, "Repeated SQL path in the vendor image.")
                    files[name] = info
            for script in manifest:
                path = script["path"]
                require(path in files, f"Missing release database script {path}.")
                data = archive.extractfile(files[path]).read()
                raw_blob = git_blob_id(data)
                comparison = "exact"
                if raw_blob != script["git_blob"]:
                    # A Windows checkout can change LF to CRLF. Only the comparison uses LF.
                    comparison = "crlf-to-lf"
                    require(git_blob_id(data.replace(b"\r\n", b"\n")) == script["git_blob"],
                            f"Image script differs from the tagged release: {path}.")
                command = ["exec", "-i", database, "psql", "-X", "-v", "ON_ERROR_STOP=1",
                           "-U", "h1", "-d", "ecrnow"]
                result = invoke(self.docker + command, data=data, check=False)
                observed.append({"path": path, "git_blob": script["git_blob"],
                                 "image_git_blob": raw_blob, "comparison": comparison,
                                 "sha256": hashlib.sha256(data).hexdigest(), "exit": result.returncode,
                                 "messages": result.stderr.decode(errors="replace")})
                save(self.output / "migration.json", observed)
                require(result.returncode == 0, f"PostgreSQL rejected {path}. See migration.json.")
        columns = self.query(database, """
          SELECT string_agg(column_name, ',' ORDER BY ordinal_position)
          FROM information_schema.key_column_usage WHERE table_schema = 'public'
          AND table_name = 'notification_context_v2' AND constraint_name = 'uc_notification_context_v2'
        """)
        require(columns == KEY_COLUMNS, "Receiver uniqueness constraint is absent or differs from the release.")
        return columns

    def wait_running(self, container, probe, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            require(self.inspect("container", container)["State"]["Running"],
                    f"Container exited during {self.stage}. Inspect the retained container on the test host.")
            if invoke(self.docker + ["exec", container] + probe, check=False).returncode == 0:
                return
            time.sleep(2)
        raise RuntimeError(f"Timed out after {seconds} seconds during {self.stage}.")

    def diagnostics(self):
        if not self.diagnostic_container:
            return {}
        try:
            identity = self.diagnostic_container
            state = self.inspect("container", identity)["State"]
            logs = invoke(self.docker + ["logs", "--tail", "80", identity], check=False)
            raw = logs.stdout + logs.stderr
            if state["Running"]:
                tail = invoke(self.docker + ["exec", identity, "tail", "-n", "120", "/tmp/h1-ecr.log"], check=False)
                raw += tail.stdout
            return {"container_id": identity, "status": state["Status"], "exit_code": state["ExitCode"],
                    "out_of_memory": state["OOMKilled"], "diagnostic": safe_diagnostics(raw)}
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError):
            return {"container_id": self.diagnostic_container, "diagnostic": "Container inspection unavailable."}

    def run(self):
        self.stage_started("Check Docker and write the intended configuration")
        self.docker = local_engine()
        path = self.output / "compose.json"
        shutil.copyfile(HERE / "compose.json", path)
        config = json.loads(path.read_bytes())
        self.compose = self.docker + ["compose", "-p", self.project, "-f", str(path)]
        invoke(self.compose + ["config", "--quiet"])
        self.stage_started("Stage the three pinned public images")
        invoke(self.compose + ["pull"], timeout=1800)
        invoke(self.docker + ["pull", "--platform", "linux/amd64", TESTER], timeout=600)
        tester_image = self.inspect("image", TESTER)
        require(tester_image["Os"] == "linux" and tester_image["Architecture"] == "amd64" and
                any(d.endswith("@" + TESTER.split("@")[1]) for d in tester_image.get("RepoDigests", [])),
                "The staged Python helper differs from its pinned Linux image.")
        self.stage_started("Create a new project and private network")
        invoke(self.compose + ["create", "--pull", "never"])
        database = self.container_id("database")
        receiver = self.container_id("receiver")
        network = self.inspect("network", self.project + "_private")
        require(network["Internal"] and network["Labels"]["com.docker.compose.project"] == self.project,
                "The test network is not private or belongs to another project.")
        self.stage_started("Start the fresh PostgreSQL database")
        self.diagnostic_container = database
        invoke(self.compose + ["start", "database"])
        self.wait_running(database, ["pg_isready", "-q", "-h", "127.0.0.1", "-U", "h1", "-d", "ecrnow"], 120)
        version = self.query(database, "SHOW server_version")
        dbname = self.query(database, "SELECT current_database()")
        require(version.split()[0] == "15.20" and dbname == "ecrnow", "Unexpected database or PostgreSQL version.")
        require(self.query(database, "SELECT count(*) FROM pg_tables WHERE schemaname='public'") == "0",
                "The new database is not empty. No migration or notification was attempted.")
        self.stage_started("Initialize the release schema and verify its duplicate constraint")
        columns = self.migrate(receiver, database)
        self.stage_started("Start the receiver and wait for its application-ready event")
        self.diagnostic_container = receiver
        invoke(self.compose + ["start", "receiver"])
        self.wait_running(receiver, ["grep", "-Fq", "Start Time of the App", "/tmp/h1-ecr.log"], 300)
        # The tagged controller rejects notices for ten seconds after the application-ready event.
        time.sleep(12)
        self.stage_started("Verify both running images and a clean lookup over loopback")
        db = self.state(database, config["services"]["database"]["image"], network)
        require(not any(db["ports"].values()), "The test database must have no host-published ports.")
        db.update(database_name=dbname, server_version=version, unique_columns=columns.split(","))
        app = self.state(receiver, config["services"]["receiver"]["image"], network)
        require(not any(app["ports"].values()), "The receiver must have no host-published ports.")
        invoke(self.docker + ["exec", receiver, "sh", "-ec",
                              "test -r /run/h1-credentials/jdbc.password && test -r /run/h1-credentials/security.key"])
        app["runtime_uid"] = invoke(self.docker + ["exec", receiver, "id", "-u"]).stdout.decode().strip()
        app["credential_files_readable"] = True
        probe = invoke(self.docker + ["run", "--rm", "--pull=never", "--platform", "linux/amd64",
                                     "--network", "container:" + receiver, TESTER,
                                     "python", "-I", "-c", SOCKET_PROBE, READINESS_PATH])
        readiness = json.loads(probe.stdout)
        require(readiness["status"] == 204 and readiness["body_bytes"] == 0,
                f"Expected an empty HTTP 204; observed HTTP {readiness['status']}.")
        receipt = {"observed_utc": now(), "project": self.project, "source_revision": SOURCE,
                   "receiver_url": "http://127.0.0.1:8081", "receiver": app, "database": db,
                   "network": {"name": network["Name"], "id": network["Id"], "internal": network["Internal"]},
                   "transport": {"mode": "container-loopback", "tester_image": TESTER, "tester_image_id": tester_image["Id"],
                                 "receiver_container_id": receiver, "local_engine": self.docker[2]},
                   "readiness": readiness}
        save(self.output / "build.json", receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New or empty deployment evidence directory")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("Use a new or empty evidence directory so earlier attempts are preserved.")
    trial = Trial(output)
    try:
        trial.run()
    except (RuntimeError, OSError, ValueError, KeyError, IndexError, tarfile.TarError, subprocess.SubprocessError) as exc:
        save(output / "state.json", {"status": "BLOCKED", "stage": trial.stage, "reason": str(exc),
                                    "project": trial.project, "utc": now(), "observations": trial.diagnostics()})
        print(f"BLOCKED. {exc}", flush=True)
        return 2
    save(output / "state.json", {"status": "READY", "project": trial.project, "utc": now()})
    print("Receiver ready. See build.json for the test address and observed deployment identity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
