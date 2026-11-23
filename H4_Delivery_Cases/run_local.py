"""Start the approved H4 run from the user's Ubuntu terminal after local authentication."""
from contextlib import contextmanager
from pathlib import Path
import signal
import subprocess
import sys

from run_h4 import ROOT, run_h3


@contextmanager
def live_gate():
    directory = ROOT.parent / "H2_Compare_Recovery/source-server"
    subprocess.run(["bash", str(directory / "prepare_runtime.sh")], check=True)
    process = subprocess.Popen([sys.executable, "-B", str(directory / "source_gate.py")],
                               stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        for line in process.stdout:
            print(line, end="", flush=True)
            if line.startswith("PASS: "):
                gate = Path(line.removeprefix("PASS: ").strip()).resolve()
                if not gate.is_relative_to(directory / "runs") or gate.name != "gate.json":
                    raise ValueError("Unexpected source evidence location.")
                yield gate
                break
        else:
            raise RuntimeError("The fresh source prerequisite checks did not pass.")
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        process.wait(timeout=30)
        process.stdout.close()


def main():
    if "--wait" in sys.argv[1:]:
        input("Press Enter when you are ready to authenticate to Ubuntu and run H4. ")
    run_h3.docker_access()
    with live_gate() as gate:
        return subprocess.run([sys.executable, "-B", str(ROOT / "run_h4.py"), "--gate", str(gate)]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
