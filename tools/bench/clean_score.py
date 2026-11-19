"""Score cleaned documents against the truth written by clean_corpus.py.

    python tools/bench/clean_score.py CORPUS_DIR RESULTS_DIR

Word error is the word-level edit distance to the truth, divided by the truth's length,
after lower-casing and removing punctuation; it is split into substituted, missing and
extra words. Text error is the same over white-space-separated tokens with capitals and
punctuation kept, after quotation marks and dashes are made uniform. "Before" is the word
error of the damaged file as given.
"""

from __future__ import annotations

import collections
import glob
import os
import re
import sys

from rapidfuzz.distance import Levenshtein


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower().replace("’", "'"))


def tokens(text: str) -> list[str]:
    for a, b in (("“", '"'), ("”", '"'), ("’", "'"), ("‘", "'"), ("—", "--")):
        text = text.replace(a, b)
    return text.split()


def rate(truth: list[str], result: list[str]) -> dict[str, float]:
    operations = Levenshtein.editops(truth, result)
    count = max(1, len(truth))
    by = collections.Counter(op.tag for op in operations)
    return {
        "error": len(operations) / count,
        "substituted": by["replace"] / count,
        "missing": by["delete"] / count,
        "extra": by["insert"] / count,
    }


def main() -> None:
    corpus, results = sys.argv[1], sys.argv[2]
    rows: dict[str, list[dict[str, float]]] = collections.defaultdict(list)
    for path in sorted(glob.glob(os.path.join(results, "*.md"))):
        match = re.search(r"([a-z-]+?)-(asr|sub|ocr)\b", os.path.basename(path))
        if not match:
            continue
        name, kind = match.groups()
        with open(os.path.join(corpus, "truth", f"{name}.truth"), encoding="utf-8") as handle:
            truth = handle.read()
        with open(glob.glob(os.path.join(corpus, f"{name}-{kind}.*"))[0], encoding="utf-8") as handle:
            source = handle.read()
        with open(path, encoding="utf-8") as handle:
            result = handle.read()
        row = rate(words(truth), words(result))
        row["text"] = rate(tokens(truth), tokens(result))["error"]
        row["before"] = rate(words(truth), words(source))["error"]
        row["words"] = float(len(words(truth)))
        rows[kind].append(row)
    print("kind | files | words | before | word error | substituted | missing | extra | text error")
    print("--- | --- | --- | --- | --- | --- | --- | --- | ---")
    for kind in ("asr", "sub", "ocr"):
        group = rows.get(kind, [])
        total = sum(row["words"] for row in group)
        if not total:
            continue
        mean = [
            sum(row[key] * row["words"] for row in group) / total
            for key in ("before", "error", "substituted", "missing", "extra", "text")
        ]
        print(f"{kind} | {len(group)} | {int(total)} | " + " | ".join(f"{value:.3f}" for value in mean))


if __name__ == "__main__":
    main()
