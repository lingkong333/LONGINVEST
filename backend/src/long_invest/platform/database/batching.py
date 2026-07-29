from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import batched

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.base import Executable

type StatementFactory[T] = Callable[[tuple[T, ...]], Executable]


@dataclass(frozen=True, slots=True)
class BatchFailure:
    first_item: int
    item_count: int
    error_type: str


@dataclass(frozen=True, slots=True)
class BatchWriteResult:
    attempted: int
    succeeded: int
    failures: tuple[BatchFailure, ...] = ()


async def execute_atomic_batches[T](
    session: AsyncSession,
    values: Sequence[T],
    *,
    batch_size: int,
    statement_factory: StatementFactory[T],
) -> BatchWriteResult:
    """Execute all batches in the caller's transaction without committing it."""
    _validate_batch_size(batch_size)
    for batch in batched(values, batch_size):
        await session.execute(statement_factory(batch))
    return BatchWriteResult(attempted=len(values), succeeded=len(values))


async def execute_isolated_batches[T](
    session: AsyncSession,
    values: Sequence[T],
    *,
    batch_size: int,
    statement_factory: StatementFactory[T],
) -> BatchWriteResult:
    """Use savepoints so a failed batch does not discard successful batches."""
    _validate_batch_size(batch_size)
    succeeded = 0
    failures: list[BatchFailure] = []
    for first_item, batch in enumerate(batched(values, batch_size)):
        try:
            async with session.begin_nested():
                await session.execute(statement_factory(batch))
        except SQLAlchemyError as exc:
            failures.append(
                BatchFailure(
                    first_item=first_item * batch_size,
                    item_count=len(batch),
                    error_type=type(exc).__name__,
                )
            )
        else:
            succeeded += len(batch)
    return BatchWriteResult(
        attempted=len(values),
        succeeded=succeeded,
        failures=tuple(failures),
    )


def _validate_batch_size(batch_size: int) -> None:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
