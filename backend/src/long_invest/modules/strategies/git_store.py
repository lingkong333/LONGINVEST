from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from git import Actor, Repo

STRATEGY_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_PROCESS_GIT_LOCK = threading.RLock()


class StrategyGitStore:
    def __init__(self, repository_root: Path) -> None:
        self._root = repository_root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._repo = (
            Repo(self._root)
            if (self._root / ".git").is_dir()
            else Repo.init(self._root)
        )

    def publish(
        self,
        *,
        strategy_id: str,
        version_no: int,
        source_code: str,
        source_code_hash: str,
        manifest: dict[str, Any],
    ) -> str:
        _validate_identity(strategy_id, version_no)
        actual_hash = hashlib.sha256(source_code.encode()).hexdigest()
        if actual_hash != source_code_hash:
            raise ValueError("source code hash mismatch")
        with _repository_lock(Path(self._repo.git_dir) / "longinvest-publish"):
            directory = self._controlled_directory(strategy_id, version_no)
            serialized_manifest = _serialize_manifest(manifest, source_code_hash)
            if directory.exists():
                return self._replay(
                    directory,
                    source_code=source_code,
                    serialized_manifest=serialized_manifest,
                )
            directory.mkdir(parents=True, exist_ok=False)
            source_path = directory / "strategy.py"
            manifest_path = directory / "manifest.json"
            source_path.write_text(source_code, encoding="utf-8", newline="\n")
            manifest_path.write_text(
                serialized_manifest,
                encoding="utf-8",
                newline="\n",
            )
            relative = directory.relative_to(self._root)
            relative_source = (relative / "strategy.py").as_posix()
            self._repo.index.add(
                [
                    relative_source,
                    (relative / "manifest.json").as_posix(),
                ]
            )
            actor = Actor("LongInvest", "longinvest@localhost")
            commit = self._repo.index.commit(
                f"Publish strategy {strategy_id} version {version_no}",
                author=actor,
                committer=actor,
            )
            if not self._path_has_hash(
                commit.hexsha, relative_source, source_code_hash
            ):
                raise RuntimeError("committed strategy hash mismatch")
            return commit.hexsha

    def _replay(
        self,
        directory: Path,
        *,
        source_code: str,
        serialized_manifest: str,
    ) -> str:
        source_path = directory / "strategy.py"
        manifest_path = directory / "manifest.json"
        if (
            not source_path.is_file()
            or not manifest_path.is_file()
            or source_path.read_text(encoding="utf-8") != source_code
            or manifest_path.read_text(encoding="utf-8") != serialized_manifest
        ):
            raise ValueError("strategy version already exists with different content")
        relative = source_path.relative_to(self._root).as_posix()
        commits = list(self._repo.iter_commits(paths=relative, max_count=1))
        if not commits:
            raise RuntimeError("strategy version exists without a Git commit")
        return commits[0].hexsha

    def read_source(self, strategy_id: str, version_no: int) -> str:
        return (
            self._controlled_directory(strategy_id, version_no) / "strategy.py"
        ).read_text(encoding="utf-8")

    def verify_source(self, commit: str, source_code_hash: str) -> bool:
        tree = self._repo.commit(commit).tree
        candidates = [
            item for item in tree.traverse() if item.path.endswith("/strategy.py")
        ]
        if not candidates:
            return False
        return any(
            hashlib.sha256(item.data_stream.read()).hexdigest() == source_code_hash
            for item in candidates
        )

    def commit_contains(self, commit: str, path: str) -> bool:
        try:
            self._repo.commit(commit).tree / path
        except KeyError:
            return False
        return True

    def _path_has_hash(self, commit: str, path: str, expected_hash: str) -> bool:
        try:
            item = self._repo.commit(commit).tree / path
        except KeyError:
            return False
        return hashlib.sha256(item.data_stream.read()).hexdigest() == expected_hash

    def _controlled_directory(self, strategy_id: str, version_no: int) -> Path:
        _validate_identity(strategy_id, version_no)
        candidate = (
            self._root / "strategies" / strategy_id / f"v{version_no}"
        ).resolve()
        if self._root not in candidate.parents:
            raise ValueError("strategy path escapes repository")
        return candidate


def _validate_identity(strategy_id: str, version_no: int) -> None:
    if STRATEGY_ID_PATTERN.fullmatch(strategy_id) is None:
        raise ValueError("invalid strategy id")
    if version_no < 1:
        raise ValueError("invalid strategy version")


def _serialize_manifest(manifest: dict[str, Any], source_code_hash: str) -> str:
    return (
        json.dumps(
            {**manifest, "source_code_hash": source_code_hash},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


@contextmanager
def _repository_lock(path: Path, timeout_seconds: float = 30.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _PROCESS_GIT_LOCK, path.open("a+b") as handle:
        _acquire_file_lock(handle, timeout_seconds)
        try:
            yield
        finally:
            _release_file_lock(handle)


def _acquire_file_lock(handle: BinaryIO, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    if os.name != "nt":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    import msvcrt

    handle.seek(0)
    if handle.read(1) == b"":
        handle.write(b"0")
        handle.flush()
    while True:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError("strategy Git repository is busy") from None
            time.sleep(0.01)


def _release_file_lock(handle: BinaryIO) -> None:
    if os.name != "nt":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
