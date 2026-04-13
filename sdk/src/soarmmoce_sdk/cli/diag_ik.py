from __future__ import annotations

from ..cli_common import run_and_print
from ..real_arm import CapabilityError


def _run_diag() -> dict[str, object]:
    raise CapabilityError("IK diagnostics are not available in the rebuilt real-arm controller.")


def main() -> None:
    run_and_print(_run_diag)


__all__ = ["main"]
