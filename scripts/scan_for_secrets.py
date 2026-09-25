"""Stdlib-only scan of tracked files for a short list of well-known credential shapes.

Not a substitute for a maintained scanner (gitleaks, trufflehog): this repository has no
CI dependency management story for a third-party GitHub Action yet, and referencing one
by an unverified tag/SHA is worse than not adding it (AGENTS.md: pin exact revisions, do
not add dependencies whose reproducibility can't be checked here). This is a minimal,
reviewable safety net for the specific failure mode this project already hit once: a raw
credential landing in tracked content. It knows a handful of well-known secret *shapes*
(AWS access keys, private key blocks, GitHub/Slack tokens, non-placeholder .env values);
it does not attempt general entropy-based detection and will miss novel secret formats.

Usage: python scripts/scan_for_secrets.py [--ref <git-ref>]
Exits non-zero and prints each match's file:line (never the matched secret text) if any
tracked file matches. `--ref` scans a git ref's tree instead of the working directory
(what CI should use, so a file already deleted from the working tree but still in
history at that ref is still caught).
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

MAX_FILE_BYTES = 2_000_000
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2", ".ttf", ".zip"}

PATTERNS = {
    "AWS access key ID": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "AWS secret access key assignment": re.compile(r"(?i)aws_secret_access_key\s*=\s*[\"']?[A-Za-z0-9/+=]{40}[\"']?"),
    "PEM private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "GitHub personal access token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
}
# A separate, narrower heuristic (checked in Python, not just regex) for a real-looking
# .env-style secret value: only outside tests/docs/examples, where a deliberately fake
# fixture value is expected and not the risk this scanner exists for.
_ENV_ASSIGNMENT = re.compile(r"(?m)^\s*[A-Z][A-Z0-9_]*(?:_KEY|_TOKEN|_SECRET|_PASSWORD)\s*=\s*[\"']?([^\s\"'#]{12,})")
_PLACEHOLDER_MARKERS = ("replace-with", "replace_with", "change_me", "changeme", "your-", "your_", "example",
                        "placeholder", "<", "${", "xxxx", "0000", "sample")
_ENV_HEURISTIC_SKIP_PATH_PARTS = ("tests/", "docs/", ".example", "README", "benchmarks/")
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9+/=_.-]+$")


def _looks_like_a_real_secret_value(value: str) -> bool:
    lowered = value.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return False
    return bool(_TOKEN_SHAPE.match(value))


def _env_style_findings(path: str, text: str) -> list[tuple[int, str]]:
    if any(part in path for part in _ENV_HEURISTIC_SKIP_PATH_PARTS):
        return []
    findings = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = _ENV_ASSIGNMENT.search(line)
        if match and _looks_like_a_real_secret_value(match.group(1)):
            findings.append((line_number, "Populated .env-style secret assignment"))
    return findings


# This file's own patterns and docstring must never trip the scanner on itself.
_SELF_PATH_SUFFIX = "scripts/scan_for_secrets.py"


def tracked_files(ref: str | None) -> list[str]:
    if ref:
        output = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref], check=True, capture_output=True, text=True).stdout
    else:
        output = subprocess.run(["git", "ls-files"], check=True, capture_output=True, text=True).stdout
    return [line for line in output.splitlines() if line]


def read_text(path: str, ref: str | None) -> str | None:
    if Path(path).suffix.lower() in SKIP_SUFFIXES:
        return None
    try:
        if ref:
            raw = subprocess.run(["git", "show", f"{ref}:{path}"], check=True, capture_output=True).stdout
        else:
            raw = Path(path).read_bytes()
    except (subprocess.CalledProcessError, OSError):
        return None
    if len(raw) > MAX_FILE_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan(ref: str | None) -> list[tuple[str, int, str]]:
    findings = []
    for path in tracked_files(ref):
        if path.endswith(_SELF_PATH_SUFFIX):
            continue
        text = read_text(path, ref)
        if text is None:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append((path, line_number, name))
        findings.extend((path, line_number, name) for line_number, name in _env_style_findings(path, text))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default=None, help="Git ref to scan instead of the working tree.")
    args = parser.parse_args()

    findings = scan(args.ref)
    if not findings:
        print("scan_for_secrets: no matches.")
        return 0
    print(f"scan_for_secrets: {len(findings)} possible credential(s) found (values withheld):")
    for path, line_number, name in findings:
        print(f"  {path}:{line_number}: {name}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
