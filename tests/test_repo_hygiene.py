"""Public-repo hygiene: no third-party client, employer or partner names in tracked files.

The names are stored ROT13-encoded so this test does not itself spell them out.
"""

import codecs
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_ENCODED = ("wrqvgv", "havcre", "gzy", "negr", "fjvfcre", "foo")
FORBIDDEN = re.compile(
    r"\b(" + "|".join(codecs.decode(n, "rot13") for n in _ENCODED) + r")\b", re.IGNORECASE
)


def _tracked_text_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    files = []
    for name in out.splitlines():
        path = REPO_ROOT / name
        if not path.is_file():
            continue
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        files.append(path)
    return files


def test_no_forbidden_names_in_tracked_files():
    hits = []
    for path in _tracked_text_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert hits == []
