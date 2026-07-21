from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

import pytest

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
    assert store.verify_source(commit, sha256(source.encode()).hexdigest())

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
