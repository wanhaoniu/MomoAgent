from __future__ import annotations

from ..cli_common import run_and_print
from ..real_arm import ValidationError


def _run_removed_command() -> dict[str, object]:
    raise ValidationError("Calibrated-home flow has been removed. Use 'home', which now returns to the startup pose.")


def main() -> None:
    run_and_print(_run_removed_command)


__all__ = ["main"]
