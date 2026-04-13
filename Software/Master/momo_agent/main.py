from __future__ import annotations

import sys
from pathlib import Path


if __package__ in (None, ""):
    package_parent = Path(__file__).resolve().parents[1]
    if str(package_parent) not in sys.path:
        sys.path.insert(0, str(package_parent))
    from momo_agent.app import main
else:
    from .app import main


if __name__ == "__main__":
    raise SystemExit(main())

