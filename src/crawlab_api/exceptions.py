from __future__ import annotations

from typing import Any


class CrawlabError(Exception):
    """Base error for the Crawlab API client."""


class CrawlabAuthError(CrawlabError):
    """Raised on HTTP 401/403 from Crawlab."""


class CrawlabNotFoundError(CrawlabError):
    """Raised on HTTP 404 from Crawlab."""


class CrawlabAPIError(CrawlabError):
    """Raised on any other non-2xx response."""

    def __init__(self, status_code: int, message: str, payload: Any = None) -> None:
        super().__init__(f"[{status_code}] {message}")
        self.status_code = status_code
        self.payload = payload
