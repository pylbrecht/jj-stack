"""GitLab REST requests above a transport: status mapping, rate-limit retries, and answer checks.

These rules hold for any `GitlabTransport`, so a transport only has to deliver the HTTP answer.
"""

from __future__ import annotations

import asyncio
import json
import logging

from pydantic import BaseModel, ValidationError

from jj_stack.forge.errors import ForgeError, ForgeErrorKind
from jj_stack.forge.gitlab.transport import GitlabResponse, GitlabTransport

logger = logging.getLogger(__name__)

_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_BACKOFF_SECONDS = 1.0
_MAX_RATE_LIMIT_WAIT_SECONDS = 60.0
_FORGE_MESSAGE_LIMIT = 300

_STATUS_ERROR_KINDS: dict[int, ForgeErrorKind] = {
    400: "invalid",
    401: "auth",
    403: "auth",
    404: "not_found",
    409: "conflict",
    422: "invalid",
    429: "rate_limited",
}


class GitlabUser(BaseModel):
    username: str


class _GitlabVersion(BaseModel):
    version: str


class GitlabClient:
    """Requests to one GitLab host through a transport."""

    def __init__(
        self,
        transport: GitlabTransport,
    ) -> None:
        self._transport = transport

    async def get_current_user(self) -> GitlabUser:
        """Return the user that the transport's login acts as."""

        return _parse(await self._request("GET", "user"), GitlabUser)

    async def get_version(self) -> str:
        """Return the host's GitLab version, such as `19.5.4-ee`."""

        return _parse(await self._request("GET", "version"), _GitlabVersion).version

    async def _request(
        self,
        method: str,
        path: str,
    ) -> GitlabResponse:
        attempt = 0
        while True:
            response = await self._transport.request(method, path)
            if response.status != 429 or attempt >= _RATE_LIMIT_RETRIES:
                return response

            wait_seconds = _retry_after_seconds(response.headers.get("retry-after"))
            if wait_seconds is None:
                wait_seconds = _RATE_LIMIT_BACKOFF_SECONDS * (2**attempt)
            wait_seconds = min(wait_seconds, _MAX_RATE_LIMIT_WAIT_SECONDS)
            logger.debug(
                "gitlab rate limit encountered: method=%s path=%s attempt=%d wait_seconds=%.3f",
                method,
                path,
                attempt + 1,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)
            attempt += 1


def _parse[ModelT: BaseModel](
    response: GitlabResponse,
    model: type[ModelT],
) -> ModelT:
    if response.status >= 300:
        raise _status_error(response)

    content_type = response.headers.get("content-type", "")
    # A host name that glab does not know can reach a login page that answers 200 with HTML,
    # so a success status alone does not prove the answer came from the GitLab API.
    if content_type.partition(";")[0].strip() != "application/json":
        raise ForgeError(
            f"request failed (the host answered with {content_type or 'no content type'}, not "
            f"a GitLab API answer; check the host name and your GitLab login)",
            kind="unavailable",
        )

    try:
        return model.model_validate_json(response.body)
    except ValidationError as error:
        raise ForgeError(
            f"request failed (unexpected GitLab answer: {error.error_count()} problems)",
            kind="unavailable",
        ) from error


def _status_error(
    response: GitlabResponse,
) -> ForgeError:
    status = response.status
    kind = _STATUS_ERROR_KINDS.get(status, "unavailable" if status >= 500 else "other")
    forge_message = _gitlab_message(response.body)
    if status == 401:
        message = "authentication failed - check the login of the configured GitLab command"
    elif status == 403:
        message = "access denied - check that your GitLab login can access the project"
    elif status == 429:
        message = "GitLab rate limit reached - rerun later"
    elif forge_message:
        message = f"request failed (GitLab {status}: {forge_message})"
    else:
        message = f"request failed (GitLab {status})"
    return ForgeError(message, kind=kind, forge_message=forge_message)


def _gitlab_message(
    body: bytes,
) -> str:
    """Return GitLab's own explanation from an error body, bounded in length.

    GitLab puts it in `message` (a string, or field errors as an object) or in `error`.
    """

    try:
        payload = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""

    explanation = payload.get("message", payload.get("error", ""))
    text = explanation if isinstance(explanation, str) else json.dumps(explanation)
    return text[:_FORGE_MESSAGE_LIMIT]


def _retry_after_seconds(
    value: str | None,
) -> float | None:
    if value is None:
        return None

    try:
        return max(float(value), 0.0)
    except ValueError:
        return None
