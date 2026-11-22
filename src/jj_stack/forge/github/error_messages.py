"""User-facing summaries for GitHub client errors."""

from __future__ import annotations

from collections.abc import Awaitable

from jj_stack.errors import CliError
from jj_stack.forge.errors import ForgeError
from jj_stack.forge.github.client import REPO_NOT_FOUND_REASON
from jj_stack.forge.models import Repo
from jj_stack.forge.protocol import Forge
from jj_stack.ui import Message, code


def repo_lookup_reason(error: ForgeError) -> str:
    """Explain a failed lookup of the repo itself, where a 404 means the repo is out of reach."""

    if error.kind == "not_found":
        return REPO_NOT_FOUND_REASON
    return error.user_facing_reason()


async def read_or_stop[T](
    read: Awaitable[T], *, message: Message, hint: Message | None = None
) -> T:
    """Await one GitHub read, turning a client failure into a stop with `message`."""

    try:
        return await read
    except ForgeError as error:
        raise CliError(message, hint=hint) from error


async def observe_github_repo(github: Forge, *, hint: Message | None = None) -> Repo:
    try:
        return await github.get_repo()
    except ForgeError as error:
        if error.kind == "not_found":
            hint = REPO_NOT_FOUND_REASON
        message = ("Could not inspect GitHub repo ", code(github.repo.full_name))
        raise CliError(message, hint=hint) from error
