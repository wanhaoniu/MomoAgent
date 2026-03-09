#!/usr/bin/env python3
"""Read current soarm101 state."""

from __future__ import annotations

from soarm101_cli_common import run_and_print
from soarm101_sdk import SoArm101Controller


def main() -> None:
    run_and_print(lambda: SoArm101Controller().read())


if __name__ == "__main__":
    main()
