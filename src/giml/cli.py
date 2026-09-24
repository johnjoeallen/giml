"""Command-line entry point (spec section 13)."""

from __future__ import annotations

import argparse
import enum
import sys
from collections.abc import Sequence

from giml import __version__


class ExitCode(enum.IntEnum):
    SUCCESS = 0
    NO_IMPROVEMENT = 1
    PREFLIGHT_REFUSAL = 2
    INELIGIBLE = 3
    INFRASTRUCTURE = 4
    CONFIGURATION = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="giml", description="Gated Increments (ML)")
    parser.add_argument("--version", action="version", version=f"giml {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help(sys.stderr)
    return ExitCode.CONFIGURATION
