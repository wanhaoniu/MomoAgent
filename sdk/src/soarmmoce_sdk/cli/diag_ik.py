from __future__ import annotations

import argparse

from ..cli_common import run_and_print
from ..real_arm import SoArmMoceController


def main() -> None:
    parser = argparse.ArgumentParser(description="Run read-only IK diagnosis for soarmMoce")
    parser.add_argument("--dx", type=float, default=0.0, help="Cartesian delta x in meters")
    parser.add_argument("--dy", type=float, default=0.0, help="Cartesian delta y in meters")
    parser.add_argument("--dz", type=float, default=0.0, help="Cartesian delta z in meters")
    parser.add_argument(
        "--frame",
        choices=["base", "urdf", "user", "tool"],
        default="base",
        help="Delta frame: base=raw URDF/sim frame, urdf=base alias, user=x forward y left z up, tool=current tool frame",
    )
    parser.add_argument("--repeats", type=int, default=12, help="Number of repeated IK solves")
    parser.add_argument("--seed-jitter-deg", type=float, default=0.1, help="Random seed perturbation on active joints")
    parser.add_argument("--random-seed", type=int, default=0, help="Deterministic RNG seed for jitter")
    args = parser.parse_args()

    run_and_print(
        lambda: SoArmMoceController().diagnose_ik(
            dx=args.dx,
            dy=args.dy,
            dz=args.dz,
            frame=args.frame,
            repeats=args.repeats,
            seed_jitter_deg=args.seed_jitter_deg,
            random_seed=args.random_seed,
        )
    )


if __name__ == "__main__":
    main()
