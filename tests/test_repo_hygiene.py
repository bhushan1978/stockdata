"""Guard rails so credentials can never be committed again (see the incident note in README)."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")

# Names a person (or the GitHub/Windows UI) might plausibly produce when copying an env file.
COPIED_ENV_NAMES = [".env", ".env.local", ".env - Copy", ".env (1)", ".env.example - Copy", "prod.env",
                    " - Copy.env", "Copy of .env", "my.env.bak", "config/.env", "a/b/ - Copy.env"]
CREDENTIAL_KEYS = ("DHAN_ACCESS_TOKEN", "DHAN_CLIENT_ID")


def git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def tracked() -> list[str]:
    return [n for n in git("ls-files").stdout.splitlines() if n]


@pytest.mark.parametrize("name", COPIED_ENV_NAMES)
def test_copied_env_files_are_ignored(name):
    # --no-index: judge the rule itself, not whether the file happens to be tracked already
    assert git("check-ignore", "--no-index", "-q", "--", name).returncode == 0, f"{name!r} would be committable"


def test_only_the_blank_template_is_allowed():
    assert git("check-ignore", "--no-index", "-q", "--", ".env.example").returncode == 1
    values = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key in CREDENTIAL_KEYS:
            values[key] = value.strip()
    assert set(values) == set(CREDENTIAL_KEYS)
    assert all(v == "" for v in values.values()), "the template must contain placeholders only"


def test_no_env_style_file_is_tracked_except_the_template():
    bad = [n for n in tracked() if n != ".env.example"
           and (n.lower().endswith(".env") or re.search(r"(^|/)\.env", n))]
    assert bad == [], f"credential-style files are tracked: {bad}"


def test_no_tracked_file_contains_a_credential_shaped_string():
    jwt = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
    # same-line only ([ \t], not \s): a blank "KEY=" followed by another line is a placeholder, not a value
    assigned = re.compile(r"\bDHAN_(?:ACCESS_TOKEN|CLIENT_ID)[ \t]*=[ \t]*[^\s#]+")
    hits = []
    for name in tracked():
        path = ROOT / name
        if not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        if jwt.search(text) or assigned.search(text):
            hits.append(name)                       # file names only: never echo the matched text
    assert hits == [], f"possible credentials in tracked files: {hits}"
