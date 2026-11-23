"""Read the GitLab project that a Git remote names."""

from __future__ import annotations

import re
from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.errors import ErrorHint, ErrorMessage
from jj_stack.forge.remote_url import parse_remote_url
from jj_stack.models.git import GitRemote

_MR_URL_PATH_RE = re.compile(r"^/(?P<path>.+)/-/merge_requests/(?P<iid>[0-9]+)/?$")


@dataclass(frozen=True, slots=True)
class GitlabProjectAddress:
    """A GitLab host and the project's full path, which may contain nested groups."""

    host: str
    path: str

    @property
    def full_name(self) -> str:
        return self.path

    def pr_url(
        self,
        pr_number: int,
    ) -> str:
        return f"https://{self.host}/{self.path}/-/merge_requests/{pr_number}"

    def head_label(
        self,
        ref: str,
    ) -> str:
        return f"{self.path}:{ref}"

    def parse_pr_url_path(
        self,
        path: str,
    ) -> tuple[str, int] | None:
        match = _MR_URL_PATH_RE.fullmatch(path)
        if match is None:
            return None

        return match["path"], int(match["iid"])


def parse_gitlab_project(
    remote: GitRemote,
) -> GitlabProjectAddress | None:
    """Return the GitLab project that both of the remote's URLs name, if they name one."""

    fetch_project = _parse_gitlab_url(remote.fetch_url)
    if fetch_project != _parse_gitlab_url(remote.push_url):
        return None

    return fetch_project


def _parse_gitlab_url(
    remote_url: str,
) -> GitlabProjectAddress | None:
    location = parse_remote_url(remote_url)
    # A project lives in at least one group or user namespace.
    if location is None or len(location.path) < 2:
        return None

    return GitlabProjectAddress(host=location.host, path="/".join(location.path))


def gitlab_project_failure(
    remote: GitRemote,
) -> tuple[ErrorMessage, ErrorHint]:
    """Say why a remote does not name one GitLab project, and what to do about it."""

    name = ui.bookmark(remote.name)
    return (
        t"Remote {name} does not have fetch and push URLs for the same GitLab project.",
        t"Point {name} at a GitLab URL such as "
        t"{ui.code('https://gitlab.example.com/group/project.git')}.",
    )
