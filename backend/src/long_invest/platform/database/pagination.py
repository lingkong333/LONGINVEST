from dataclasses import dataclass

from sqlalchemy import Select

MAX_PAGE_SIZE = 200


@dataclass(frozen=True, slots=True)
class PageWindow:
    page: int
    page_size: int

    def __post_init__(self) -> None:
        if self.page < 1 or not 1 <= self.page_size <= MAX_PAGE_SIZE:
            raise ValueError(
                f"page must be positive and page_size must be between 1 and "
                f"{MAX_PAGE_SIZE}"
            )

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    def apply[T](self, statement: Select[tuple[T]]) -> Select[tuple[T]]:
        return statement.offset(self.offset).limit(self.page_size)
