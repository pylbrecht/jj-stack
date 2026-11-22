"""Retry the local update after a merge while GitHub finishes rewriting the remaining PRs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import jj_stack.console as console
from jj_stack.errors import DriftError

# GitHub rewrites the PRs above a merged one several seconds after reporting the merge, and a
# PR's reported head trails its branch meanwhile.
_CATCH_UP_SECONDS = 15.0
_CATCH_UP_POLL_INTERVAL_SECONDS = 3.0


async def retry_while_github_catches_up(update: Callable[[], Awaitable[int]]) -> int:
    """Rerun the local update while GitHub has not yet caught up with its own rewrite."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CATCH_UP_SECONDS
    waited = False
    while True:
        try:
            return await update()
        except DriftError as error:
            if error.condition != "pr_head_lagging" or loop.time() >= deadline:
                raise
        if not waited:
            console.output(
                "GitHub is still updating the remaining PRs; waiting for it to finish."
            )
            waited = True
        with console.spinner(description="Waiting for GitHub to update the remaining PRs"):
            await asyncio.sleep(_CATCH_UP_POLL_INTERVAL_SECONDS)
