"""Local-only model artifact verification helpers."""

import hashlib
from pathlib import Path


def directory_sha256(root: Path) -> str:
    if not root.is_dir() or any(item.is_symlink() for item in root.rglob("*")):
        raise RuntimeError("Model artifact must contain regular local files")
    files = sorted(root.rglob("*"))
    if not files:
        raise RuntimeError("Model artifact must contain regular local files")
    digest = hashlib.sha256()
    for item in files:
        if not item.is_file():
            continue
        relative = item.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def verified_model_directory(raw_path: str | None, expected_sha256: str | None) -> Path:
    if not raw_path or not expected_sha256 or len(expected_sha256) != 64:
        raise RuntimeError("Local model path and SHA-256 must be configured")
    candidate = Path(raw_path)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise RuntimeError("Model artifact path must be absolute and local")
    root = candidate.resolve(strict=True)
    if directory_sha256(root).lower() != expected_sha256.lower():
        raise RuntimeError("Local model artifact checksum mismatch")
    return root
