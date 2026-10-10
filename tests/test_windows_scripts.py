"""Static checks of the Windows scripts. They cannot be executed on a Linux machine, but the mistakes that usually break a
.bat file can be found by reading it: line endings, characters cmd.exe treats as operators inside messages, labels, scripts
they call, and whether every Python invocation is accepted by the real argument parsers (the logic itself lives in Python and
is tested for real in test_web_start_stop.py)."""
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NORMAL = ["start_stockdata.bat", "stop_stockdata.bat"]
DEV = ["tools/dev/build_frontend.bat", "tools/dev/frontend_dev.bat"]
ALL = NORMAL + DEV


def test_normal_operation_needs_exactly_two_scripts_in_the_repository_root():
    assert sorted(p.name for p in ROOT.glob("*.bat")) == sorted(NORMAL)
    for gone in ("setup_stockdata.bat", "start_backend.bat", "start_frontend_dev.bat", "build_frontend.bat"):
        assert not (ROOT / gone).exists(), f"{gone} must not be in the normal workflow"
    assert (ROOT / "tools" / "dev" / "README.md").exists()


@pytest.mark.parametrize("name", ALL)
def test_script_is_plain_ascii_with_crlf_and_starts_quietly(name):
    raw = (ROOT / name).read_bytes()
    raw.decode("ascii")                                            # no smart quotes / BOM
    assert b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n"), "every line must end in CRLF"
    assert raw.lstrip().lower().startswith(b"@echo off") and raw.endswith(b"\r\n")


@pytest.mark.parametrize("name", ALL)
def test_echo_lines_contain_no_cmd_operators_and_no_parentheses_inside_blocks(name):
    for n, line in enumerate((ROOT / name).read_text().splitlines(), 1):
        stripped = line.strip()
        if not stripped.lower().startswith("echo"):
            continue
        message = re.sub(r"\^.", "", stripped)                     # escaped characters are fine
        assert not re.search(r"[<>|&]", message), f"{name}:{n} would redirect or chain: {stripped}"
        if line.startswith((" ", "\t")):                           # indented = inside a ( ... ) block, where ) ends the block
            assert "(" not in message and ")" not in message, f"{name}:{n} parenthesis inside a block: {stripped}"


@pytest.mark.parametrize("name", ALL)
def test_labels_and_called_scripts_exist(name):
    text = (ROOT / name).read_text()
    labels = set(re.findall(r"^:(\w+)", text, re.M))
    for target in re.findall(r"\bgoto\s+(\w+)", text, re.I):
        assert target.lower() == "eof" or target in labels, f"{name}: goto {target} has no label"
    for called in re.findall(r"(?:call)\s+(\w+\.bat)", text, re.I):
        assert (ROOT / Path(name).parent / called).exists(), f"{name} calls missing {called}"


def test_every_python_invocation_is_accepted_by_the_real_parsers():
    from stockdata.web.__main__ import build_parser as web_parser
    from stockdata.web.launcher import build_parser as launcher_parser
    seen = {"launcher": 0, "web": 0}
    for name in ALL:
        for line in (ROOT / name).read_text().splitlines():
            m = re.search(r"-m stockdata\.web\.launcher\s*(.*)$", line)
            if m:
                launcher_parser().parse_args(shlex.split(m.group(1).replace("%*", "9000")))
                seen["launcher"] += 1
                continue
            m = re.search(r"-m stockdata\.web\s+(.*)$", line)
            if m:
                args = m.group(1).replace("%*", "--pause-downloads --wait 60").replace("%PORT%", "8765")
                web_parser().parse_args(shlex.split(args))
                seen["web"] += 1
    assert seen == {"launcher": 1, "web": 2}, seen                 # start (launcher); stop, and the developer script's serve --dev


def test_the_start_script_covers_the_required_steps_and_never_closes_silently():
    text = (ROOT / "start_stockdata.bat").read_text()
    assert "3.10" in text and "py -%%V" in text and "python -c" in text            # looks for a suitable Python, says which is needed
    assert "stockdata.web.launcher" in text and r".venv\Scripts\python.exe" in text
    failing = text.split(":launch", 1)[1].split(":done", 1)[0]
    assert "pause" in failing and "exit /b %RC%" in failing                          # a failure waits for a key and keeps the exit code
    assert "pause" in text.split(":launch", 1)[0]                                    # so does 'no Python found'
    assert "-m venv" not in text and "pip install" not in text                       # setup logic lives in tested Python, not in the batch file


def test_the_stop_script_passes_every_choice_through_and_reports_failures():
    text = (ROOT / "stop_stockdata.bat").read_text()
    assert r".venv\Scripts\python.exe" in text and "-m stockdata.web stop %*" in text
    assert "pause" in text and "exit /b %RC%" in text


def test_package_files_and_the_prebuilt_interface_are_consistent():
    assert (ROOT / "requirements-web.txt").exists()
    assert (ROOT / ".gitattributes").read_text().count("*.bat text eol=crlf") == 1
    assert (ROOT / "webui" / "dist" / "index.html").exists(), "the prebuilt interface must ship with the project"
    from stockdata.web import bootstrap
    assert bootstrap.check_frontend(ROOT / "webui" / "dist")["ok"]
