from __future__ import annotations

import sys
from pathlib import Path


def ensure_repo_root_on_sys_path() -> None:
    """Ensure the repo root is importable when running `python scripts/xxx.py`.

    When executing a script by path, Python sets `sys.path[0]` to that script's
    directory (e.g. `scripts/`) and does not reliably include the workspace root.
    This helper makes imports like `import mp_power...` work consistently.
    """

    root = Path(__file__).resolve().parents[1]
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
