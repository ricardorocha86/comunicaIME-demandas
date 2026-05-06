from __future__ import annotations

from typing import Any

import requests


class HttpClient:
    def __init__(self, timeout_seconds: int = 30) -> None:
        self.timeout_seconds = timeout_seconds
        self._session = requests.Session()

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        timeout = kwargs.pop("timeout", self.timeout_seconds)
        return self._session.get(url, timeout=timeout, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        timeout = kwargs.pop("timeout", self.timeout_seconds)
        return self._session.post(url, timeout=timeout, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> requests.Response:
        timeout = kwargs.pop("timeout", self.timeout_seconds)
        return self._session.patch(url, timeout=timeout, **kwargs)

