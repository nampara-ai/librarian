"""Build the corrupt-and-restore corpus for the fast pipeline.

Truth is the public-domain reference text of the cases in examples/editorial_holdout_v*.json.
The cases are grouped by category into documents. Each document is damaged in three ways,
with a fixed seed: as a speech transcript, as a subtitle file of the same transcript, and
as scanner output. Cases at odd positions form the development half and cases at even
positions the holdout half.

    python tools/bench/clean_corpus.py --out DIR --split dev

DIR receives NAME-asr.txt, NAME-sub.srt and NAME-ocr.txt for every document, and the
subfolder DIR/truth receives NAME.truth.
Clean the three damaged files with `librarian clean DIR -o RESULTS --body-only` and score
the results with clean_score.py.
"""

from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import json
import os
import random
import re

FILLERS = ["um", "uh", "you know", "i mean", "like", "sort of", "uh", "um", "so", "right"]
SOUND_ALIKE = {
    "their": "there", "there": "their", "to": "too", "too": "to", "its": "it's",
    "it's": "its", "whether": "weather", "than": "then", "then": "than",
    "affect": "effect", "effect": "affect", "principle": "principal", "accept": "except",
    "would": "wood", "whole": "hole", "been": "bin", "great": "grate", "knew": "new",
    "right": "write", "one": "won", "here": "hear", "by": "buy", "which": "witch",
    "where": "wear", "for": "four", "no": "know", "know": "no", "some": "sum",
    "reason": "raisin", "nature": "nay sure", "species": "spee sees",
    "government": "govern mint", "constitution": "constant tuition",
    "liberty": "libber tea", "opinion": "a pinion", "natural": "natch oral",
    "selection": "see lection", "society": "so sigh a tea",
    "individual": "in the visual", "power": "pour", "people": "peep hole",
    "general": "gen role", "character": "care actor", "family": "fam lee",
    "manner": "manor", "principles": "principals", "authority": "a thorough tea",
}  # fmt: skip
LOOK_ALIKE = [
    ("rn", "m"), ("m", "rn"), ("l", "1"), ("I", "l"), ("O", "0"), ("e", "c"), ("h", "b"),
    ("cl", "d"), ("fi", "ﬁ"), ("u", "ii"), ("a", "o"), ("t", "l"), ("ri", "n"),
    ("s", "5"), ("B", "8"),
]  # fmt: skip


def speech(text: str, rng: random.Random) -> str:
    """No capitals, no punctuation, no paragraphs; fillers, stutters, false starts, sound-alike words."""
    out: list[str] = []
    for word in re.findall(r"[A-Za-z0-9']+", text.lower()):
        if rng.random() < 0.035:
            out.append(rng.choice(FILLERS))
        if word in SOUND_ALIKE and rng.random() < 0.30:
            out.extend(SOUND_ALIKE[word].split())
            continue
        if rng.random() < 0.012 and len(word) > 5:
            word = word[:-2] if word.endswith(("ed", "es", "ly")) else word + "s"
        out.append(word)
        if rng.random() < 0.02:
            out.append(word)
        if rng.random() < 0.006 and len(out) > 3:
            out.extend(out[-3:-1])
    return " ".join(out)


def subtitles(text: str, rng: random.Random) -> str:
    """The speech transcript cut into numbered, timed cues."""

    def stamp(seconds: float) -> str:
        return f"{int(seconds // 3600):02d}:{int(seconds % 3600 // 60):02d}:{seconds % 60:06.3f}".replace(".", ",")

    words = text.split()
    cues, at, clock, number = [], 0, 0.0, 1
    while at < len(words):
        count = rng.randint(5, 11)
        length = count * 0.38
        cues.append(f"{number}\n{stamp(clock)} --> {stamp(clock + length)}\n{' '.join(words[at : at + count])}\n")
        at += count
        clock += length + rng.random() * 0.4
        number += 1
    return "\n".join(cues)


def scanner(text: str, rng: random.Random, width: int = 62) -> str:
    """Look-alike characters, split and joined words, hard line breaks with hyphens, stray marks, running heads."""
    words = []
    for word in text.split():
        if rng.random() < 0.06:
            a, b = rng.choice(LOOK_ALIKE)
            if a in word:
                word = word.replace(a, b, 1)
        if rng.random() < 0.015 and len(word) > 6:
            cut = rng.randint(2, len(word) - 2)
            word = word[:cut] + " " + word[cut:]
        if rng.random() < 0.01:
            word += rng.choice([" .", " ,", " |", " ~", "’"])
        words.append(word)
    joined, at = [], 0
    while at < len(words):
        if at + 1 < len(words) and rng.random() < 0.012:
            joined.append(words[at] + words[at + 1])
            at += 2
        else:
            joined.append(words[at])
            at += 1
    lines, line = [], ""
    for word in joined:
        if len(line) + len(word) + 1 > width:
            room = width - len(line)
            if len(word) > 7 and rng.random() < 0.5 and room > 4 and word.isalpha():
                cut = max(2, min(len(word) - 2, room - 2))
                lines.append(line + (" " if line else "") + word[:cut] + "-")
                line = word[cut:]
            else:
                lines.append(line)
                line = word
        else:
            line += (" " if line else "") + word
    lines.append(line)
    page = []
    for number, text_line in enumerate(lines):
        if number and number % 23 == 0:
            page += ["", str(rng.randint(12, 340)), "THE COLLECTED WORKS", ""]
        page.append(text_line)
    return "\n".join(page)


def documents(examples: str, split: str) -> dict[str, str]:
    groups: dict[str, list[str]] = collections.defaultdict(list)
    position = 0
    for path in sorted(glob.glob(os.path.join(examples, "editorial_holdout_v*.json"))):
        with open(path, encoding="utf-8") as handle:
            for case in json.load(handle)["cases"]:
                half = "dev" if position % 2 else "holdout"
                position += 1
                if split in ("all", half):
                    groups[case["category"]].append(case["reference"].replace("\r", ""))
    return {name: "\n\n".join(parts) for name, parts in groups.items() if len(parts) >= 4}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--examples", default=os.path.join(os.path.dirname(__file__), "..", "..", "examples"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    args = parser.parse_args()
    os.makedirs(os.path.join(args.out, "truth"), exist_ok=True)
    for name, truth in documents(args.examples, args.split).items():
        # A fixed seed makes the corpus repeatable. Nothing here is a secret.
        rng = random.Random(hashlib.sha256(f"long:{args.split}:{name}".encode()).digest())  # noqa: S311
        spoken = speech(truth, rng)
        files = {
            os.path.join("truth", f"{name}.truth"): truth,
            f"{name}-asr.txt": spoken,
            f"{name}-sub.srt": subtitles(spoken, rng),
            f"{name}-ocr.txt": scanner(truth, rng),
        }
        for file, text in files.items():
            with open(os.path.join(args.out, file), "w", encoding="utf-8") as handle:
                handle.write(text)
        print(f"{name}: {len(truth.split())} words")


if __name__ == "__main__":
    main()
