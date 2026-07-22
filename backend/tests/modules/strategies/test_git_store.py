from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

import pytest
from git.index.base import IndexFile

from long_invest.modules.strategies import git_store
from long_invest.modules.strategies.git_store import StrategyGitStore


def test_publish_commits_only_controlled_strategy_files(tmp_path):
    store = StrategyGitStore(tmp_path / "strategies")
    source = "def calculate_targets(history, params, context):\n    return {}\n"

    commit = store.publish(
        strategy_id="2f26c42f-c1f6-47af-9ee8-1dd6e23f63cc",
        version_no=1,
        source_code=source,
        source_code_hash=sha256(source.encode()).hexdigest(),
        manifest={"environment_version": "python-3.12"},
    )

    assert len(commit) == 40
    assert store.read_source("2f26c42f-c1f6-47af-9ee8-1dd6e23f63cc", 1) == source
    assert store.verify_source(
        strategy_id="2f26c42f-c1f6-47af-9ee8-1dd6e23f63cc",
        version_no=1,
        commit=commit,
        source_code_hash=sha256(source.encode()).hexdigest(),
    )

    replay = store.publish(
        strategy_id="2f26c42f-c1f6-47af-9ee8-1dd6e23f63cc",
        version_no=1,
        source_code=source,
        source_code_hash=sha256(source.encode()).hexdigest(),
        manifest={"environment_version": "python-3.12"},
    )
    assert replay == commit


def test_publish_rejects_hash_mismatch(tmp_path):
    store = StrategyGitStore(tmp_path / "strategies")

    with pytest.raises(ValueError, match="hash"):
        store.publish(
            strategy_id="2f26c42f-c1f6-47af-9ee8-1dd6e23f63cc",
            version_no=1,
            source_code="source",
            source_code_hash="0" * 64,
            manifest={},
        )


def test_retry_continues_after_git_commit_failed_with_files_already_written(
    tmp_path, monkeypatch
):
    store = StrategyGitStore(tmp_path / "strategies")
    source = "source"
    original_commit = IndexFile.commit
    calls = 0

    def fail_once(index, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("interrupted before commit")
        return original_commit(index, *args, **kwargs)

    monkeypatch.setattr(IndexFile, "commit", fail_once)
    values = {
        "strategy_id": "2f26c42f-c1f6-47af-9ee8-1dd6e23f63cc",
        "version_no": 1,
        "source_code": source,
        "source_code_hash": sha256(source.encode()).hexdigest(),
        "manifest": {},
    }

    with pytest.raises(OSError, match="interrupted"):
        store.publish(**values)
    commit = store.publish(**values)

    assert len(commit) == 40


def test_retry_rebuilds_controlled_file_after_interrupted_write(tmp_path, monkeypatch):
    store = StrategyGitStore(tmp_path / "strategies")
    original_write = Path.write_text
    calls = 0

    def fail_once(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("interrupted write")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_once)
    values = {
        "strategy_id": "2f26c42f-c1f6-47af-9ee8-1dd6e23f63cc",
        "version_no": 1,
        "source_code": "source",
        "source_code_hash": sha256(b"source").hexdigest(),
        "manifest": {},
    }

    with pytest.raises(OSError, match="interrupted"):
        store.publish(**values)
    commit = store.publish(**values)

    assert len(commit) == 40


def test_retry_restages_after_index_add_failure(tmp_path, monkeypatch):
    store = StrategyGitStore(tmp_path / "strategies")
    original_add = IndexFile.add
    calls = 0

    def fail_once(index, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("interrupted add")
        return original_add(index, *args, **kwargs)

    monkeypatch.setattr(IndexFile, "add", fail_once)
    values = {
        "strategy_id": "2f26c42f-c1f6-47af-9ee8-1dd6e23f63cc",
        "version_no": 1,
        "source_code": "source",
        "source_code_hash": sha256(b"source").hexdigest(),
        "manifest": {},
    }

    with pytest.raises(OSError, match="interrupted"):
        store.publish(**values)
    commit = store.publish(**values)

    assert len(commit) == 40


def test_same_version_with_different_content_is_never_rewritten(tmp_path):
    store = StrategyGitStore(tmp_path / "strategies")
    strategy_id = "2f26c42f-c1f6-47af-9ee8-1dd6e23f63cc"
    store.publish(
        strategy_id=strategy_id,
        version_no=1,
        source_code="first",
        source_code_hash=sha256(b"first").hexdigest(),
        manifest={},
    )

    with pytest.raises(ValueError, match="different content"):
        store.publish(
            strategy_id=strategy_id,
            version_no=1,
            source_code="second",
            source_code_hash=sha256(b"second").hexdigest(),
            manifest={},
        )


def test_unrelated_staged_file_is_never_included_in_publication(tmp_path):
    store = StrategyGitStore(tmp_path / "strategies")
    unrelated = store._root / "unrelated.txt"
    unrelated.write_text("do not commit", encoding="utf-8")
    store._repo.index.add(["unrelated.txt"])

    with pytest.raises(RuntimeError, match="unrelated staged"):
        store.publish(
            strategy_id="2f26c42f-c1f6-47af-9ee8-1dd6e23f63cc",
            version_no=1,
            source_code="source",
            source_code_hash=sha256(b"source").hexdigest(),
            manifest={},
        )


def test_commit_verification_is_bound_to_exact_strategy_version_path(tmp_path):
    store = StrategyGitStore(tmp_path / "strategies")
    source_hash = sha256(b"same").hexdigest()
    first = "2f26c42f-c1f6-47af-9ee8-1dd6e23f63cc"
    second = "dc647b11-d72e-49f9-a1db-3742a2c43572"
    commit = store.publish(
        strategy_id=first,
        version_no=1,
        source_code="same",
        source_code_hash=source_hash,
        manifest={},
    )

    assert not store.verify_source(
        strategy_id=second,
        version_no=1,
        commit=commit,
        source_code_hash=source_hash,
    )


def test_concurrent_publications_each_commit_their_own_source(tmp_path):
    store = StrategyGitStore(tmp_path / "strategies")
    releases = (
        ("2f26c42f-c1f6-47af-9ee8-1dd6e23f63cc", "source-one"),
        ("dc647b11-d72e-49f9-a1db-3742a2c43572", "source-two"),
    )

    def publish(item):
        strategy_id, source = item
        commit = store.publish(
            strategy_id=strategy_id,
            version_no=1,
            source_code=source,
            source_code_hash=sha256(source.encode()).hexdigest(),
            manifest={},
        )
        return strategy_id, commit

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, releases))

    for strategy_id, commit in results:
        path = f"strategies/{strategy_id}/v1/strategy.py"
        assert store.commit_contains(commit, path)


def test_process_lock_wait_uses_same_repository_deadline(tmp_path, monkeypatch):
    class BusyLock:
        def __init__(self):
            self.timeouts = []

        def acquire(self, *, timeout):
            self.timeouts.append(timeout)
            return False

        def release(self):
            raise AssertionError("an unacquired lock must not be released")

    lock = BusyLock()
    monkeypatch.setattr(git_store, "_PROCESS_GIT_LOCK", lock)

    with (
        pytest.raises(TimeoutError, match="repository is busy"),
        git_store._repository_lock(tmp_path / "repo.lock", timeout_seconds=0.01),
    ):
        raise AssertionError("busy lock must not enter critical section")

    assert lock.timeouts == [0.01]


@pytest.mark.parametrize("strategy_id", ["../escape", "main; rm -rf /"])
def test_store_rejects_user_controlled_paths(tmp_path, strategy_id):
    store = StrategyGitStore(tmp_path / "strategies")

    with pytest.raises(ValueError, match="strategy id"):
        store.publish(
            strategy_id=strategy_id,
            version_no=1,
            source_code="source",
            source_code_hash=sha256(b"source").hexdigest(),
            manifest={},
        )
