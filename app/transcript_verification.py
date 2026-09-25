"""Word-level transcript accuracy check: did local STT capture every word?

Second-layer verification for Task 3 step 5b's pending release gate ("record actual
WER, role/timestamp behaviour") and the operations guide's golden-set protocol: compare
this application's redacted/hypothesis transcript against a human reference transcript
of the same call, and report exactly which words were missed, invented, or misheard, not
only an aggregate error rate.

Privacy: this module does no I/O of its own beyond its CLI reading local files the
caller names explicitly. It never fetches, logs, or persists a transcript anywhere; the
caller is responsible for not committing real call transcripts to source control
(AGENTS.md: no customer recordings, raw transcripts or sensitive fixtures in Git; use
synthetic data until real-data processing and retention policies are approved).

Redaction note: comparing a raw reference against a redacted hypothesis will report a
mismatch at every redacted span (a person's name, a phone number) that is not a real STT
error. Either compare pre-redaction STT output locally and never persist or transmit it
elsewhere, or apply the same redaction to the reference before comparing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, TypedDict

MAX_WORDS = 20_000  # Bounds the O(reference * hypothesis) alignment table.
_WORD = re.compile(r"[^\s]+")
_STRIP_PUNCTUATION = re.compile(r"^[^\w']+|[^\w']+$")


class AlignmentOp(TypedDict):
    op: Literal["match", "substitute", "delete", "insert"]
    reference: str | None
    hypothesis: str | None


class VerificationInputError(ValueError):
    """The supplied transcript text or utterance list does not meet this tool's bounds."""


def normalise_words(text: str) -> list[str]:
    """Lowercase, strip leading/trailing punctuation per token, drop empties.

    This is a WER normalisation choice, not a transcription contract: "don't" and
    "dont" compare equal-ish only insofar as punctuation is stripped, not spelling.
    Numerals are compared as written ("2" != "two"). Keep this consistent between the
    reference and hypothesis you pass in, or differences become normalisation noise
    rather than real STT errors.
    """
    if not isinstance(text, str):
        raise VerificationInputError("transcript text must be a string")
    words = [_STRIP_PUNCTUATION.sub("", token).lower() for token in _WORD.findall(text)]
    return [word for word in words if word]


def align_words(reference_words: Sequence[str], hypothesis_words: Sequence[str]) -> list[AlignmentOp]:
    """Minimum-edit-distance word alignment (Levenshtein), returned as an ordered op list.

    Substitution, insertion and deletion all cost 1. When one reference word sits next
    to a run of several extra hypothesis words, every placement of the substitute
    within that run costs the same total, so ties are real, not just a formality: at a
    tie this prefers, in order, match, insert, delete, substitute — walking the
    backtrack from the end, that peels off trailing extra hypothesis words as insertions
    first and lets the substitute land next to the nearest actual mismatch, instead of
    pairing a reference word with a hypothesis word several positions away. Without this
    order, an equal-cost alignment can pair unrelated distant words as a "substitution"
    while calling the word that actually replaced the reference word an "insertion" -
    numerically identical WER, but a misleading diff for a human reading the report.
    """
    if len(reference_words) > MAX_WORDS or len(hypothesis_words) > MAX_WORDS:
        raise VerificationInputError(f"transcript exceeds the bounded {MAX_WORDS}-word alignment limit")
    reference_count, hypothesis_count = len(reference_words), len(hypothesis_words)
    # table[i][j] = edit distance between reference[:i] and hypothesis[:j].
    table = [[0] * (hypothesis_count + 1) for _ in range(reference_count + 1)]
    for i in range(1, reference_count + 1):
        table[i][0] = i
    for j in range(1, hypothesis_count + 1):
        table[0][j] = j
    for i in range(1, reference_count + 1):
        for j in range(1, hypothesis_count + 1):
            if reference_words[i - 1] == hypothesis_words[j - 1]:
                table[i][j] = table[i - 1][j - 1]
            else:
                table[i][j] = 1 + min(table[i - 1][j - 1], table[i - 1][j], table[i][j - 1])

    ops: list[AlignmentOp] = []
    i, j = reference_count, hypothesis_count
    while i > 0 or j > 0:
        if i > 0 and j > 0 and reference_words[i - 1] == hypothesis_words[j - 1] and table[i][j] == table[i - 1][j - 1]:
            ops.append({"op": "match", "reference": reference_words[i - 1], "hypothesis": hypothesis_words[j - 1]})
            i, j = i - 1, j - 1
        elif j > 0 and table[i][j] == table[i][j - 1] + 1:
            ops.append({"op": "insert", "reference": None, "hypothesis": hypothesis_words[j - 1]})
            j -= 1
        elif i > 0 and table[i][j] == table[i - 1][j] + 1:
            ops.append({"op": "delete", "reference": reference_words[i - 1], "hypothesis": None})
            i -= 1
        else:
            ops.append({"op": "substitute", "reference": reference_words[i - 1], "hypothesis": hypothesis_words[j - 1]})
            i, j = i - 1, j - 1
    ops.reverse()
    return ops


def word_error_rate(reference_text: str, hypothesis_text: str) -> dict[str, Any]:
    """WER = (substitutions + deletions + insertions) / reference word count.

    `wer` is None when the reference has no words (the standard ratio is undefined);
    `matches`/`missed_words`/`invented_words`/`misheard_pairs` are always populated so a
    zero-word reference still gives a usable, non-crashing report.
    """
    reference_words, hypothesis_words = normalise_words(reference_text), normalise_words(hypothesis_text)
    ops = align_words(reference_words, hypothesis_words)
    counts = {"match": 0, "substitute": 0, "delete": 0, "insert": 0}
    for op in ops:
        counts[op["op"]] += 1
    reference_word_count = len(reference_words)
    errors = counts["substitute"] + counts["delete"] + counts["insert"]
    return {
        "reference_word_count": reference_word_count,
        "hypothesis_word_count": len(hypothesis_words),
        "matches": counts["match"],
        "substitutions": counts["substitute"],
        "deletions": counts["delete"],
        "insertions": counts["insert"],
        "wer": (errors / reference_word_count) if reference_word_count else None,
        "missed_words": [op["reference"] for op in ops if op["op"] == "delete"],
        "invented_words": [op["hypothesis"] for op in ops if op["op"] == "insert"],
        "misheard_pairs": [(op["reference"], op["hypothesis"]) for op in ops if op["op"] == "substitute"],
        "ops": ops,
    }


def _validated_hypothesis_utterances(utterances: Any) -> list[dict]:
    if not isinstance(utterances, Sequence) or isinstance(utterances, (str, bytes)):
        raise VerificationInputError("hypothesis utterances must be a list")
    validated = []
    for item in utterances:
        if not isinstance(item, Mapping) or "text" not in item or "start_ms" not in item or "end_ms" not in item or "id" not in item:
            raise VerificationInputError("each hypothesis utterance needs id, start_ms, end_ms and text")
        validated.append({"id": item["id"], "start_ms": item["start_ms"], "end_ms": item["end_ms"], "text": item["text"]})
    return validated


def compare_transcript(reference_text: str, hypothesis_utterances: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """WER over the whole call, plus which utterance (or gap between two) each miss falls near.

    Missed reference words (deletions) are attributed to the gap immediately before the
    next hypothesis utterance that still has an unconsumed word — i.e. "STT never
    produced this word, and here is roughly where in the call it belongs" — rather than
    silently folding them into the overall count with no actionable location.
    """
    utterances = _validated_hypothesis_utterances(hypothesis_utterances)
    hypothesis_text = " ".join(str(item["text"]) for item in utterances)
    result = word_error_rate(reference_text, hypothesis_text)

    # Map each hypothesis word position (in the concatenated text) to its utterance.
    word_owner: list[dict] = []
    for utterance in utterances:
        for _ in normalise_words(str(utterance["text"])):
            word_owner.append(utterance)

    per_utterance: dict[str, dict] = {
        utterance["id"]: {"id": utterance["id"], "start_ms": utterance["start_ms"], "end_ms": utterance["end_ms"],
                          "missed_words_before": [], "substitutions": [], "invented_words": []}
        for utterance in utterances
    }
    unattached_missed_words: list[str] = []
    hypothesis_index = 0
    next_owner_index = 0
    for op in result["ops"]:
        if op["op"] == "match" or op["op"] == "substitute":
            owner = word_owner[hypothesis_index]
            if op["op"] == "substitute":
                per_utterance[owner["id"]]["substitutions"].append((op["reference"], op["hypothesis"]))
            hypothesis_index += 1
            next_owner_index = hypothesis_index
        elif op["op"] == "insert":
            owner = word_owner[hypothesis_index]
            per_utterance[owner["id"]]["invented_words"].append(op["hypothesis"])
            hypothesis_index += 1
            next_owner_index = hypothesis_index
        else:  # delete: a reference word with no hypothesis word consumed here yet.
            if next_owner_index < len(word_owner):
                per_utterance[word_owner[next_owner_index]["id"]]["missed_words_before"].append(op["reference"])
            else:
                unattached_missed_words.append(op["reference"])  # Missed after the call's last utterance.

    result["per_utterance"] = list(per_utterance.values())
    result["missed_words_after_last_utterance"] = unattached_missed_words
    return result


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("reference", type=Path, help="Path to a local plain-text reference transcript.")
    parser.add_argument("hypothesis", type=Path, help="Path to the STT hypothesis: plain text, or JSON utterances with --utterances.")
    parser.add_argument("--utterances", action="store_true", help="Treat hypothesis as JSON: a list of {id, start_ms, end_ms, text}.")
    args = parser.parse_args(argv)

    reference_text = args.reference.read_text(encoding="utf-8")
    if args.utterances:
        result = compare_transcript(reference_text, _load_json(args.hypothesis))
    else:
        result = word_error_rate(reference_text, args.hypothesis.read_text(encoding="utf-8"))

    wer_display = f"{result['wer']:.1%}" if result["wer"] is not None else "undefined (empty reference)"
    print(f"WER: {wer_display}  (reference words: {result['reference_word_count']}, "
          f"matches: {result['matches']}, substitutions: {result['substitutions']}, "
          f"deletions: {result['deletions']}, insertions: {result['insertions']})")
    if result["missed_words"]:
        print(f"Missed words (STT never produced these): {result['missed_words']}")
    if result["invented_words"]:
        print(f"Invented words (STT produced these, reference doesn't have them): {result['invented_words']}")
    if result["misheard_pairs"]:
        print(f"Misheard (reference -> STT): {result['misheard_pairs']}")
    if "per_utterance" in result:
        for utterance in result["per_utterance"]:
            if utterance["missed_words_before"] or utterance["substitutions"] or utterance["invented_words"]:
                print(f"  utterance {utterance['id']} [{utterance['start_ms']}-{utterance['end_ms']}ms]: "
                      f"missed_before={utterance['missed_words_before']} substitutions={utterance['substitutions']} "
                      f"invented={utterance['invented_words']}")
        if result["missed_words_after_last_utterance"]:
            print(f"  after the last utterance: missed={result['missed_words_after_last_utterance']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
