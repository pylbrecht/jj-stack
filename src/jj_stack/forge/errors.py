"""Forge-neutral request failures."""

from __future__ import annotations

from typing import Literal

from jj_stack.errors import EXIT_FORGE, SummarizedError

# `not_found` 404 or a missing object; `auth` 401 or 403; `rate_limited` a refused request that
# waiting clears; `conflict` 409; `invalid` 400 or 422; `unavailable` 5xx, a transport failure, or
# a response with no usable answer; `other` any other status.
type ForgeErrorKind = Literal[
    "auth",
    "conflict",
    "invalid",
    "not_found",
    "other",
    "rate_limited",
    "unavailable",
]


class ForgeError(SummarizedError):
    """A forge request failed or returned an unusable response.

    `message` is the rendered reason the adapter builds when it raises. `forge_message` is the
    server's own explanation, empty when it gave none.
    """

    exit_code = EXIT_FORGE

    def __init__(
        self,
        message: str,
        *,
        kind: ForgeErrorKind,
        forge_message: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.forge_message = forge_message

    def user_facing_reason(self) -> str:
        return self.message
