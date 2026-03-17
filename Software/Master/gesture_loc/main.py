from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info < (3, 10):
    raise SystemExit(
        "gesture_loc requires Python 3.10+. "
        "Use: PYTHONNOUSERSITE=1 /home/sunyuan/miniconda3/envs/gestureloc/bin/python "
        "Software/Master/gesture_loc/main.py --config Software/Master/gesture_loc/configs/default.yaml"
    )

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gesture_tracking.main import cli_main


def main() -> None:
    cli_main()


if __name__ == "__main__":
    main()
