import os
from pathlib import Path
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
