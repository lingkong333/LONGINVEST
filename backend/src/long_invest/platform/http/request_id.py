import re
from uuid import uuid4

from asgi_correlation_id import correlation_id

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_PATTERN = re.compile(r"^req_[A-Za-z0-9_-]{8,60}$", re.ASCII)

def create_request_id() -> str:
    return f"req_{uuid4().hex}"


def is_valid_request_id(candidate: str) -> bool:
    return REQUEST_ID_PATTERN.fullmatch(candidate) is not None


def get_request_id() -> str:
    request_id = correlation_id.get()
    return request_id if request_id is not None else create_request_id()
