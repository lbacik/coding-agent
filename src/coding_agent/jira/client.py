"""The thin transport under the Jira Cloud tracker adapter.

Carries the credential and the site base URL; callers inspect the returned
``requests.Response`` themselves, since classification and the port
operations each need different things from the same status codes and
headers. Mirrors ``coding_agent.github.client``: no retry, no translation,
no Jira object model here.

Authentication is the Jira Cloud pair: the account's email plus an API
token as HTTP Basic auth, or a bearer token where no email is configured.
The secret itself is never logged or copied into an exception: only status
codes, paths and header values travel outward.
"""

from __future__ import annotations

from typing import Any

import requests


class JiraClient:
    """Thin wrapper around the Jira Cloud REST API (``/rest/api/3``).

    ``base_url`` is the site root (``https://<site>.atlassian.net``); paths
    are the API paths below it. ``email`` plus ``token`` authenticates as
    HTTP Basic auth (the Jira Cloud email-plus-API-token pair); without an
    email the token travels as a bearer token instead.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str,
        email: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._email = email
        self._session = session or requests.Session()

    @property
    def base_url(self) -> str:
        """The site root this client talks to (no trailing slash)."""
        return self._base_url

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """One authenticated call; the response comes back to read."""
        url = f"{self._base_url}{path}" if path.startswith("/") else f"{self._base_url}/{path}"
        headers = {"Accept": "application/json"}
        extra_headers: dict[str, str] | None = kwargs.pop("headers", None)
        if extra_headers:
            headers.update(extra_headers)
        auth: Any = None
        if self._email is not None:
            auth = (self._email, self._token)
        else:
            headers["Authorization"] = f"Bearer {self._token}"
        return self._session.request(method, url, headers=headers, auth=auth, **kwargs)

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        """One authenticated ``GET``."""
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        """One authenticated ``POST``."""
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> requests.Response:
        """One authenticated ``PUT``."""
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        """One authenticated ``DELETE``."""
        return self.request("DELETE", path, **kwargs)
