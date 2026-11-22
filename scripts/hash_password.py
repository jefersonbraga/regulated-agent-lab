"""Print a ``DEMO_USERS`` entry for one demo user (see ``regulated_agent_lab.api.hash_password``).

    uv run --project backend python scripts/hash_password.py alice                # role "both"
    uv run --project backend python scripts/hash_password.py bob --role approver

The same command is installed as ``ral-hash-password`` (``cd backend && uv run ral-hash-password
alice``; in the API image: ``ral-hash-password alice``). The password is read without echo
(asked twice), never taken from the command line, and never printed.
"""

from __future__ import annotations

import getpass  # noqa: F401  (the prompt the module uses; tests patch getpass.getpass)
import sys
from pathlib import Path

# Without the installed package (a plain `python scripts/...`), import it from backend/src.
_SRC = Path(__file__).resolve().parent.parent / "backend" / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

from regulated_agent_lab.api.hash_password import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
