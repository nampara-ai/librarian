"""Validate changelog state before publishing a release tag."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def validate_changelog_ready(changelog: str, version: str) -> None:
    """Raise ValueError if changelog entries are not staged for the release version."""
    normalized_version = version.removeprefix("v")
    sections = _sections(changelog)
    if normalized_version not in sections:
        raise ValueError(f"CHANGELOG.md has no section for {normalized_version}")
    unreleased = sections.get("Unreleased", "")
    unreleased_entries = [
        line
        for line in unreleased.splitlines()
        if line.lstrip().startswith(("- ", "* "))
    ]
    if unreleased_entries:
        raise ValueError("CHANGELOG.md still has Unreleased entries")


def _sections(changelog: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in changelog.splitlines():
        if line.startswith("## "):
            title = line.removeprefix("## ").strip()
            current = title.split(" - ", maxsplit=1)[0].strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {title: "\n".join(lines).strip() for title, lines in sections.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--changelog", default="CHANGELOG.md")
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)

    changelog = Path(args.changelog).read_text(encoding="utf-8")
    validate_changelog_ready(changelog, args.version)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc
