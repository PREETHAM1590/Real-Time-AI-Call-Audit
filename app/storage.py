from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import stat
from uuid import uuid4


class LocalPrivateStorage:
    """Private local object store for development; production uses private mounted storage."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, audio: bytes) -> str:
        key = f"{uuid4().hex}.audio"
        target = self.root / key
        temporary = self.root / f".{key}.tmp"
        try:
            with temporary.open("xb") as output:
                output.write(audio)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
        return key

    def delete(self, key: str) -> None:
        if Path(key).name != key:
            raise ValueError("invalid storage key")
        (self.root / key).unlink(missing_ok=True)

    def get(self, key: str, *, max_bytes: int) -> bytes:
        """Read one bounded object without accepting path components from callers."""
        if Path(key).name != key or not key.endswith(".audio") or max_bytes <= 0:
            raise ValueError("invalid storage key or byte limit")
        target = self.root / key
        if target.is_symlink():
            raise ValueError("audio object must not be a symlink")
        with target.open("rb") as source:
            data = source.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("audio object exceeds configured byte limit")
        return data

    def size(self, key: str, *, max_bytes: int) -> int:
        if Path(key).name != key or not key.endswith(".audio") or max_bytes <= 0:
            raise ValueError("invalid storage key or byte limit")
        target = self.root / key
        if target.is_symlink():
            raise ValueError("audio object must not be a symlink")
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError("audio object is not a bounded regular file")
        return info.st_size

    def iter_range(self, key: str, start: int, end: int, *, chunk_bytes: int = 256 * 1024):
        """Yield a bounded byte range from a validated private object key."""
        if Path(key).name != key or not key.endswith(".audio") or start < 0 or end < start or chunk_bytes <= 0:
            raise ValueError("invalid storage key or byte range")
        target = self.root / key
        if target.is_symlink():
            raise ValueError("audio object must not be a symlink")
        with target.open("rb") as source:
            source.seek(start)
            remaining = end - start + 1
            while remaining:
                block = source.read(min(chunk_bytes, remaining))
                if not block:
                    return
                remaining -= len(block)
                yield block

    def delete_orphans(self, referenced: set[str], *, older_than: timedelta = timedelta(days=1)) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - older_than.total_seconds()
        removed = 0
        for item in self.root.glob("*.audio"):
            if item.name not in referenced:
                try:
                    if item.stat().st_mtime < cutoff:
                        item.unlink(missing_ok=True)
                        removed += 1
                except FileNotFoundError:
                    pass
        return removed
