"""Export exact registry package pins from uv.lock."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path
from typing import Any


def export_constraints(lock_text: str) -> str:
    """Return requirements-style exact pins for registry packages in uv.lock."""
    payload = tomllib.loads(lock_text)
    packages = payload.get("package")
    if not isinstance(packages, list):
        raise ValueError("uv.lock does not contain package entries")

    pins: dict[str, str] = {}
    for package in packages:
        if not isinstance(package, dict):
            continue
        source = package.get("source")
        if not _is_registry_source(source):
            continue
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            continue
        normalized = name.lower().replace("_", "-")
        pins[normalized] = f"{normalized}=={version}"

    if not pins:
        raise ValueError("uv.lock does not contain registry package pins")

    header = [
        "# Generated from uv.lock.",
        "# Exact third-party package pins for release reproducibility.",
        "",
    ]
    return "\n".join([*header, *[pins[name] for name in sorted(pins)], ""])


def _is_registry_source(source: Any) -> bool:
    return isinstance(source, dict) and isinstance(source.get("registry"), str)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lockfile", default="uv.lock")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    lock_text = Path(args.lockfile).read_text(encoding="utf-8")
    constraints = export_constraints(lock_text)
    Path(args.output).write_text(constraints, encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc
