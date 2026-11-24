"""Межпроцессная блокировка экземпляра на одном локальном файле SQLite."""

import sys
from pathlib import Path
from typing import BinaryIO, Self

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class InstanceLock:
    """Не допускать два экземпляра приложения на одном локальном томе."""

    def __init__(self, database_path: Path) -> None:
        self.path = database_path.with_suffix(database_path.suffix + ".lock")
        self.handle: BinaryIO | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("Эта база уже используется другим экземпляром приложения") from None
        self.handle = handle
        return self

    def __exit__(self, *args: object) -> None:
        if self.handle is not None:
            if sys.platform == "win32":
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None
