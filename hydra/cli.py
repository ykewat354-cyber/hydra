"""HYDRA CLI entry point (skeleton; full command surface lands in later phases)."""
from __future__ import annotations

import sys

from . import __version__


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] in (["--version"], ["-V"]):
        print(f"hydra {__version__}")
        return 0
    print("hydra: skeleton build — the full CLI arrives with the next build phase.")
    return 0
