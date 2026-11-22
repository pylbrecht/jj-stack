"""Read the GitLab project that a Git remote names."""

from __future__ import annotations

from dataclasses import dataclass

from jj_stack.forge.remote_url import parse_remote_url
from jj_stack.models.git import GitRemote


@dataclass(frozen=True, slots=True)
class GitlabProjectAddress:
    """A GitLab host and the project's full path, which may contain nested groups."""

    host: str
    path: str

    @property
    def full_name(self) -> str:
        return self.path


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
