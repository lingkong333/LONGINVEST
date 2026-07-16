from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class SuccessEnvelope(BaseModel):
    success: Literal[True]
    code: str
    message: str
    request_id: str
    server_time: datetime


class Pagination(BaseModel):
    page: int
    page_size: int
    total: int
