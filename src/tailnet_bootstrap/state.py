"""Private, atomic, process-safe storage for nonsecret bootstrap progress."""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable


class StateError(RuntimeError):
    """State cannot be read or written safely."""


_SENSITIVE_KEY = re.compile(r"(?:password|passwd|secret|credential|cookie|private[_-]?key|(?:^|[_-])token(?:$|[_-])|authorization|auth[_-]?key|api[_-]?key|access[_-]?token)", re.I)


def _validate_nonsecret(value: Any, path: str = "state") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise StateError("State keys must be strings")
            if _SENSITIVE_KEY.search(key):
                raise StateError(f"Credential-like state key is prohibited: {path}.{key}")
            _validate_nonsecret(item, f"{path}.{key}")
    elif isinstance(value, list):
        for item in value:
            _validate_nonsecret(item, path)
    elif isinstance(value, float) and not math.isfinite(value):
        raise StateError("State must contain finite JSON numbers only")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise StateError("State must contain JSON data only")


class StateStore:
    """Store JSON metadata only; callers must never supply credential values.

    Every operation takes the same flock. ``update`` performs a read/modify/write
    transaction, so callers should use it for concurrent step progress updates.
    """

    def __init__(self, directory: str | os.PathLike[str]):
        self.directory = Path(directory).expanduser().absolute()
        self.path = self.directory / "state.json"
        self.lock_path = self.directory / "state.lock"
        self._prepare_directory()

    def _prepare_directory(self) -> None:
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise StateError("State directory must be a real directory owned by this user")
            os.chmod(self.directory, 0o700)
        except OSError as exc:
            raise StateError("Cannot prepare private state directory") from exc

    @contextlib.contextmanager
    def _locked(self):
        self._prepare_directory()
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                os.close(fd)
                raise StateError("State lock must be a regular file owned by this user")
            os.fchmod(fd, 0o600)
        except OSError as exc:
            raise StateError("Cannot open state lock safely") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self) -> dict[str, Any]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise StateError("Cannot open state safely") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise StateError("State must be a regular file owned by this user")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                data = json.load(handle)
            if not isinstance(data, dict):
                raise StateError("State root must be a JSON object")
            _validate_nonsecret(data)
            return data
        except (ValueError, UnicodeError) as exc:
            raise StateError("State is not valid JSON; preserve it for review before restarting") from exc
        finally:
            if fd >= 0:
                os.close(fd)

    def _write(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise StateError("State root must be a JSON object")
        _validate_nonsecret(data)
        try:
            encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        except (ValueError, TypeError) as exc:
            raise StateError("State must contain finite JSON data only") from exc
        temp_path: str | None = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".state-", dir=self.directory)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise StateError("Cannot atomically save state") from exc
        finally:
            if temp_path is not None:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp_path)

    @contextlib.contextmanager
    def transaction(self):
        """Hold the lock across a mutation with durable intermediate checkpoints.

        The yielded transaction has ``data`` and ``checkpoint()``. The final data
        is saved on successful exit. A raised exception leaves the last durable
        checkpoint intact, which is useful before an external preference change.
        """
        with self._locked():
            store = self
            class Transaction:
                def __init__(self):
                    self.data = store._read()
                def checkpoint(self):
                    store._write(self.data)
            transaction = Transaction()
            yield transaction
            transaction.checkpoint()

    def load(self) -> dict[str, Any]:
        with self._locked():
            return self._read()

    def save(self, data: dict[str, Any]) -> None:
        with self._locked():
            self._write(data)

    def update(self, mutator: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict[str, Any]:
        with self._locked():
            data = self._read()
            replacement = mutator(data)
            if replacement is not None:
                data = replacement
            self._write(data)
            return data
