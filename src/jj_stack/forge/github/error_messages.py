"""User-facing summaries for GitHub client errors."""

from __future__ import annotations

from collections.abc import Awaitable

from jj_stack.errors import CliError
from jj_stack.forge.errors import ForgeError
from jj_stack.forge.github.client import REPO_NOT_FOUND_REASON
from jj_stack.forge.models import Repo
from jj_stack.forge.protocol import Forge, RepoAddress
from jj_stack.models.git import GitRemote
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


def remote_and_github_unavailable_messages(
    *,
    github_error: Message | None,
    github_repo: RepoAddress | None,
    remote: GitRemote | None,
    remote_error: Message | None,
) -> tuple[Message, ...]:
    """Render the repo-level warning lines for an unavailable remote or GitHub target."""

    messages: list[Message] = []
    if remote is None:
        messages.append("No Git remote is configured." if remote_error is None else remote_error)
    if github_error is not None:
        if github_repo is None:
            messages.append(("GitHub unavailable: ", github_error))
        else:
            messages.append(
                ("GitHub unavailable for ", code(github_repo.full_name), ": ", github_error)
            )
    return tuple(messages)
