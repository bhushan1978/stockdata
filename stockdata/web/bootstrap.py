"""Environment checks that need nothing but the Python standard library (so they work before any package is installed).

    python -m stockdata.web.bootstrap check     # report missing / too-old packages and a missing prebuilt interface
    python -m stockdata.web.bootstrap ensure    # the same, then `pip install -r requirements-web.txt` for what is missing

Used by the launcher on every start (a no-op when everything is present) and by the System page's dependency health.
"""
from __future__ import annotations

import argparse
import importlib.metadata as md
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIN_PYTHON = (3, 10)
REQUIREMENTS = "requirements-web.txt"
_REQ = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(?:(>=|==|>|<=|<|~=)\s*([A-Za-z0-9_.\-]+))?")


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:4])


def parse_requirements(path: Path, _seen: set[Path] | None = None) -> list[tuple[str, str | None, str | None]]:
    """[(package, operator, version)] following ``-r other.txt`` includes."""
    path = Path(path).resolve()
    seen = _seen if _seen is not None else set()
    if path in seen or not path.exists():
        return []
    seen.add(path)
    out: list[tuple[str, str | None, str | None]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement ")):
            out += parse_requirements(path.parent / line.split(None, 1)[1].strip(), seen)
            continue
        if line.startswith("-"):
            continue
        m = _REQ.match(line)
        if m:
            out.append((m.group(1), m.group(2), m.group(3)))
    return out


def _satisfied(installed: str, op: str | None, wanted: str | None) -> bool:
    if op is None or wanted is None:
        return True
    a, b = version_tuple(installed), version_tuple(wanted)
    return {">=": a >= b, ">": a > b, "<=": a <= b, "<": a < b, "==": a == b, "~=": a >= b}[op]


def check_requirements(requirements: Path | None = None) -> list[dict]:
    rows = []
    for name, op, wanted in parse_requirements(requirements or REPO_ROOT / REQUIREMENTS):
        try:
            have = md.version(name)
        except md.PackageNotFoundError:
            have = None
        rows.append({"package": name, "required": f"{op}{wanted}" if op else "any", "installed": have,
                     "ok": have is not None and _satisfied(have, op, wanted)})
    return rows


def check_frontend(dist: Path | None = None) -> dict:
    """The prebuilt interface: index.html plus every asset it references."""
    dist = Path(dist) if dist else REPO_ROOT / "webui" / "dist"
    index = dist / "index.html"
    if not index.exists():
        return {"ok": False, "path": str(dist), "problems": [f"{index} is missing"]}
    problems = []
    for ref in re.findall(r'(?:src|href)="(/assets/[^"]+)"', index.read_text(encoding="utf-8", errors="replace")):
        if not (dist / ref.lstrip("/")).exists():
            problems.append(f"{ref} is referenced by index.html but missing")
    return {"ok": not problems, "path": str(dist), "problems": problems}


def python_ok() -> bool:
    return sys.version_info[:2] >= MIN_PYTHON


def environment(requirements: Path | None = None, dist: Path | None = None) -> dict:
    pkgs = check_requirements(requirements)
    return {"python": ".".join(str(n) for n in sys.version_info[:3]), "python_ok": python_ok(), "min_python": ".".join(map(str, MIN_PYTHON)),
            "executable": sys.executable, "packages": pkgs, "packages_ok": all(p["ok"] for p in pkgs), "frontend": check_frontend(dist)}


def ensure(requirements: Path | None = None, *, install: bool = True, out=print) -> int:
    """0 when the environment is complete (installing what is missing first, if allowed)."""
    req = requirements or REPO_ROOT / REQUIREMENTS
    if not python_ok():
        out(f"[StockData] Python {'.'.join(map(str, MIN_PYTHON))} or newer is required; this is {sys.version.split()[0]}.")
        return 1
    missing = [p for p in check_requirements(req) if not p["ok"]]
    if missing and install:
        out("[StockData] Installing the required Python packages (first start only; this needs an internet connection):")
        for p in missing:
            out(f"    {p['package']} {p['required']}  (installed: {p['installed'] or 'no'})")
        rc = subprocess.call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(req)])
        if rc != 0:
            out("[StockData] pip could not install the packages. Check the internet connection (or proxy settings) and start again.")
            return 1
        missing = [p for p in check_requirements(req) if not p["ok"]]
    if missing:
        out("[StockData] Missing or outdated packages: " + ", ".join(f"{p['package']} (need {p['required']})" for p in missing))
        return 1
    fe = check_frontend()
    if not fe["ok"]:
        out("[StockData] The prebuilt web interface is not available: " + "; ".join(fe["problems"]))
        out("            It ships in the repository under webui/dist. Restore it (for example `git checkout -- webui/dist`) or download the project again.")
        out("            Node.js is only needed by developers who change the interface (tools/dev/build_frontend.bat).")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m stockdata.web.bootstrap")
    ap.add_argument("action", choices=["check", "ensure"])
    ap.add_argument("--requirements", type=Path)
    args = ap.parse_args(argv)
    if args.action == "check":
        rc = ensure(args.requirements, install=False)
        if rc == 0:
            print("[StockData] The environment is complete.")
        return rc
    return ensure(args.requirements, install=True)


if __name__ == "__main__":
    raise SystemExit(main())
