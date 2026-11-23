"""Shared synchronization for comments managed by submit."""

from __future__ import annotations

from jj_stack.errors import CliError
from jj_stack.forge.errors import ForgeError
from jj_stack.forge.models import PRComment
from jj_stack.forge.protocol import Forge
from jj_stack.formatting import format_pr_number


async def upsert_managed_comment(
    *,
    body: str,
    existing_comment: PRComment | None,
    github_client: Forge,
    label: str,
    pr_number: int,
) -> None:
    if existing_comment is not None and existing_comment.body == body:
        return
    action = "create" if existing_comment is None else "update"
    try:
        if existing_comment is None:
            await github_client.create_issue_comment(
                issue_number=pr_number,
                body=body,
            )
        else:
            await github_client.update_issue_comment(
                issue_number=pr_number,
                comment_id=existing_comment.id,
                body=body,
            )
    except ForgeError as error:
        article = "a " if action == "create" else ""
        pr_label = format_pr_number(pr_number, repo=github_client.repo)
        raise CliError(
            f"Could not {action} {article}{label} for pull request {pr_label}"
        ) from error
