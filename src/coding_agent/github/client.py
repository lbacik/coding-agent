from __future__ import annotations

from typing import Any

import requests

DEFAULT_BASE_URL = "https://api.github.com"
API_VERSION = "2022-11-28"


class GitHubClient:
    """Thin wrapper around the GitHub REST API.

    Carries auth and the required headers; callers inspect the returned
    `requests.Response` themselves, since preflight and startup checks each
    need different things from the same status codes and headers.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        session: requests.Session | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._session = session or requests.Session()

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = f"{self._base_url}{path}" if path.startswith("/") else f"{self._base_url}/{path}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        extra_headers: dict[str, str] | None = kwargs.pop("headers", None)
        if extra_headers:
            headers.update(extra_headers)
        return self._session.request(method, url, headers=headers, **kwargs)

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("PATCH", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("DELETE", path, **kwargs)
