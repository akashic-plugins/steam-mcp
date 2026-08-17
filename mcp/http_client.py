from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json


class SteamApiError(RuntimeError):
    """Raised when the Steam Web API request fails."""


@dataclass(frozen=True)
class SteamHttpResponse:
    format: str
    status_code: int
    data: Any


class HttpClient:
    def __init__(
        self,
        base_url: str = "https://api.steampowered.com",
        timeout: float = 15.0,
        config_path: str = "steam_mcp_config.json",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.config_path = self._resolve_config_path(config_path)

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        response_format: str = "json",
        *,
        require_key: bool = False,
    ) -> SteamHttpResponse:
        query_params = dict(params or {})
        query_params["format"] = response_format

        if require_key:
            api_key = self._load_api_key()
            query_params["key"] = api_key

        url = f"{self.base_url}/{path.lstrip('/')}?{urlencode(query_params)}"
        request = Request(url, method="GET", headers={"Accept": "application/json, text/plain;q=0.9, */*;q=0.8"})

        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                return SteamHttpResponse(
                    format=response_format,
                    status_code=response.status,
                    data=self._parse_body(body=body, response_format=response_format),
                )
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SteamApiError(f"Steam API returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise SteamApiError(f"Steam API request failed: {exc.reason}") from exc

    def _load_api_key(self) -> str:
        if not self.config_path.exists():
            raise SteamApiError(
                f"Steam API key is required. Create `{self.config_path}` and set `steam_api_key`."
            )

        try:
            config = json.loads(self.config_path.read_text())
        except json.JSONDecodeError as exc:
            raise SteamApiError(f"Invalid config file: `{self.config_path}` is not valid JSON.") from exc

        api_key = str(config.get("steam_api_key", "")).strip()
        if not api_key:
            raise SteamApiError(
                f"Steam API key is required. Set `steam_api_key` in `{self.config_path}`."
            )
        return api_key

    @staticmethod
    def _resolve_config_path(config_path: str) -> Path:
        path = Path(config_path)
        if path.is_absolute():
            return path
        return (Path(__file__).resolve().parent / path).resolve()

    @staticmethod
    def _parse_body(body: str, response_format: str) -> Any:
        if response_format == "json":
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise SteamApiError("Steam API returned invalid JSON") from exc
        return body

    def post_form(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        response_format: str = "json",
        *,
        require_key: bool = False,
    ) -> SteamHttpResponse:
        form_params = dict(params or {})
        form_params["format"] = response_format

        if require_key:
            api_key = self._load_api_key()
            form_params["key"] = api_key

        body = urlencode(form_params).encode("utf-8")
        url = f"{self.base_url}/{path.lstrip('/')}"
        request = Request(
            url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                response_body = response.read().decode("utf-8")
                return SteamHttpResponse(
                    format=response_format,
                    status_code=response.status,
                    data=self._parse_body(body=response_body, response_format=response_format),
                )
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SteamApiError(f"Steam API returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise SteamApiError(f"Steam API request failed: {exc.reason}") from exc

    def get_text(self, url: str, timeout: float | None = None) -> str:
        request = Request(url, method="GET", headers={"Accept": "application/xml, text/xml, text/html;q=0.9, */*;q=0.8"})

        try:
            with urlopen(request, timeout=timeout or self.timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SteamApiError(f"Steam page returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise SteamApiError(f"Steam page request failed: {exc.reason}") from exc

    def get_json_url(self, url: str, timeout: float | None = None) -> Any:
        body = self.get_text(url, timeout=timeout)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise SteamApiError("Steam page returned invalid JSON") from exc
