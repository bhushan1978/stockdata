"""First-start and every-start launcher. Standard library only: it runs with whatever Python the user has installed,
before anything is installed.

    python -m stockdata.web.launcher [--port N] [--no-browser] [--foreground]

1. checks the Python version (3.10 or newer),
2. creates the virtual environment ``.venv`` if it does not exist,
3. installs / verifies the required packages inside it (a quick no-op once they are present),
4. verifies the prebuilt web interface,
5. hands over to ``python -m stockdata.web start`` which opens an already-running application or starts one in the background,
   moves to a free port when the default is taken, and opens the browser.

Every failure prints what went wrong and what to do about it, and returns a non-zero exit code (the .bat file then waits for a key).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIN_PYTHON = (3, 10)
VENV = REPO_ROOT / ".venv"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def venv_python(venv: Path = VENV) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def python_version_of(exe: Path) -> tuple[int, int] | None:
    try:
        out = subprocess.run([str(exe), "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"], capture_output=True, text=True, timeout=60)
        a, b = out.stdout.split()[:2]
        return int(a), int(b)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def ensure_venv(venv: Path = VENV) -> Path | None:
    """The virtual environment's interpreter (created on first start), or None after printing why not."""
    exe = venv_python(venv)
    if exe.exists():
        ver = python_version_of(exe)
        if ver is None or ver < MIN_PYTHON:
            say(f"[StockData] The existing virtual environment ({venv}) is unusable or older than Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}.")
            say("            Delete the folder .venv and start again; it will be recreated.")
            return None
        return exe
    say(f"[StockData] First start: creating the virtual environment in {venv} ...")
    rc = subprocess.call([sys.executable, "-m", "venv", str(venv)])
    if rc != 0 or not exe.exists():
        say("[StockData] Could not create the virtual environment.")
        say("            Make sure Python was installed with its standard components (the 'venv' module and pip), then start again.")
        return None
    return exe


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m stockdata.web.launcher")
    ap.add_argument("port_pos", nargs="?", type=int, metavar="PORT", help="preferred port (same as --port)")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--foreground", action="store_true", help="keep the server in this console (for troubleshooting)")
    ap.add_argument("--check-only", action="store_true", help="verify the environment and stop (do not start the application)")
    ap.add_argument("--data-dir")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.port = args.port or args.port_pos

    if sys.version_info[:2] < MIN_PYTHON:
        say(f"[StockData] Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required; this is Python {sys.version.split()[0]}.")
        say("            Install a current version from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and start again.")
        return 1
    exe = ensure_venv()
    if exe is None:
        return 1
    # packages (installed on first start) and the prebuilt interface
    rc = subprocess.call([str(exe), "-m", "stockdata.web.bootstrap", "ensure"], cwd=str(REPO_ROOT))
    if rc != 0:
        return 1
    if args.check_only:
        say("[StockData] The environment is complete.")
        return 0
    cmd = [str(exe), "-m", "stockdata.web"]
    if args.data_dir:
        cmd += ["--data-dir", args.data_dir]
    cmd += ["start"]
    if args.port:
        cmd += ["--port", str(args.port)]
    if args.no_browser:
        cmd.append("--no-browser")
    if args.foreground:
        cmd.append("--foreground")
    return subprocess.call(cmd, cwd=str(REPO_ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
