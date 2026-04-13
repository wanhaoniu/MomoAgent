from __future__ import annotations

from ..cli_common import run_and_print
from ..real_arm import SoArmMoceController


def _run_state() -> dict[str, object]:
    with SoArmMoceController() as arm:
        return arm.get_state()


def main() -> None:
    run_and_print(_run_state)


__all__ = ["main"]
