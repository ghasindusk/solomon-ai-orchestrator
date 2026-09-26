"""Canonical `octavryn` command (v0.5 R7). Same commands as `solomon`,
without the deprecation notice."""

from __future__ import annotations

from solomon.cli import main as _main


def main(argv: list[str] | None = None) -> int:
    return _main(argv, prog="octavryn")


if __name__ == "__main__":
    raise SystemExit(main())
