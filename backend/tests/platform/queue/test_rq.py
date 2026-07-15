from unittest.mock import Mock
from uuid import uuid4

from long_invest.platform.queue.rq import RqQueuePublisher


def test_rq_publishes_through_the_composed_job_worker(monkeypatch) -> None:
    queue = Mock()
    queue.enqueue.return_value.id = "outbox-1"
    queue_factory = Mock(return_value=queue)
    monkeypatch.setattr("long_invest.platform.queue.rq.Queue", queue_factory)
    publisher = RqQueuePublisher.__new__(RqQueuePublisher)
    publisher._redis = Mock()

    job_id = uuid4()
    outbox_id = uuid4()
    publisher._publish_sync("maintenance", outbox_id, job_id, 60)

    assert queue.enqueue.call_args.args[0] == (
        "long_invest.entrypoints.job_worker.execute_job"
    )
