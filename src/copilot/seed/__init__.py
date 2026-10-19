"""Deterministic seed data for the fictional store.

Run `python -m copilot.seed` to (re)generate data/generated. The output is
byte-identical on every run, so it is committed and CI checks it is up to date.
"""

from copilot.seed.build import SEED, build_dataset

__all__ = ["SEED", "build_dataset"]
