"""Split a Git remote URL into the host and repo path that a forge address is built from."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class RemoteLocation:
    """The host and slash-separated repo path segments of one Git remote URL."""

    host: str
    path: tuple[str, ...]


def parse_remote_url(remote_url: str) -> RemoteLocation | None:
    """Read the host and repo path from an HTTP(S), SSH, or scp-style Git remote URL."""

    parsed = urlparse(remote_url)
    if parsed.scheme in {"http", "https", "ssh"} and parsed.hostname:
        host = parsed.hostname
        raw_path = parsed.path
    elif _looks_like_scp_remote(remote_url):
        prefix, _, raw_path = remote_url.partition(":")
        host = prefix.rpartition("@")[2]
    else:
        return None

    # A trailing slash has to go before `.git` does, or `repo.git/` keeps the suffix and
    # every request 404s against `<owner>/<repo>.git`.
    normalized_path = raw_path.strip("/").removesuffix(".git")
    return RemoteLocation(
        host=host,
        path=tuple(part for part in normalized_path.split("/") if part),
    )


def _looks_like_scp_remote(url: str) -> bool:
    """Return whether a remote uses Git's scp-style host:path shorthand."""

    prefix, separator, suffix = url.partition(":")
    if not separator or not prefix or not suffix:
        return False
    # `scheme://…` is a URL, not scp-style `host:path`. urlparse already handled the schemes
    # that carry a forge host; one with no host at all, like `file:///srv/repo.git`, names
    # no forge repo and must not be read as `srv/repo`.
    if suffix.startswith("//"):
        return False
    if "/" in prefix or "\\" in prefix:
        return False
    # Reject Windows drive paths like C:/repo.git.
    if len(prefix) == 1 and prefix.isalpha():
        return False
    return True
