"""Extract release notes for one changelog version."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def extract_release_notes(changelog: str, version: str) -> str:
    """Return the changelog section for a tag or bare version."""
    normalized_version = version.removeprefix("v")
    lines = changelog.splitlines()
    start_index: int | None = None
    heading = ""
    for index, line in enumerate(lines):
        if not line.startswith("## "):
            continue
        title = line.removeprefix("## ").strip()
        section_version = title.split(" - ", maxsplit=1)[0].strip()
        if section_version == normalized_version:
            start_index = index + 1
            heading = title
            break
    if start_index is None:
        raise ValueError(f"CHANGELOG.md has no section for {normalized_version}")

    end_index = len(lines)
    for index in range(start_index, len(lines)):
        if lines[index].startswith("## "):
            end_index = index
            break

    body = "\n".join(lines[start_index:end_index]).strip()
    if not body:
        raise ValueError(f"CHANGELOG.md section for {normalized_version} is empty")
    return f"## {heading}\n\n{body}\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--changelog", default="CHANGELOG.md")
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    changelog = Path(args.changelog).read_text(encoding="utf-8")
    notes = extract_release_notes(changelog, args.version)
    Path(args.output).write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc
