"""GitHub wire models for merge queue, check, and review-thread evidence."""

from __future__ import annotations

from pydantic import AliasPath, BaseModel, Field, model_validator

from jj_stack.forge.models import Check, ReviewThread


class GithubCheckStateCount(BaseModel):
    state: str
    count: int


class GithubCheckCounts(BaseModel):
    total: int = Field(alias="totalCount")
    runs: tuple[GithubCheckStateCount, ...] | None = Field(alias="checkRunCountsByState")
    statuses: tuple[GithubCheckStateCount, ...] | None = Field(alias="statusContextCountsByState")

    @property
    def remaining(self) -> int | None:
        return self._count({"EXPECTED", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING"})

    @property
    def failed(self) -> int | None:
        return self._count(
            {
                "FAILURE",
                "ERROR",
                "ACTION_REQUIRED",
                "CANCELLED",
                "TIMED_OUT",
                "STALE",
                "STARTUP_FAILURE",
            }
        )

    def _count(self, states: set[str]) -> int | None:
        if self.runs is None or self.statuses is None:
            return None
        return sum(item.count for item in (*self.runs, *self.statuses) if item.state in states)


class GithubMergeQueueEntry(BaseModel):
    position: int | None = None
    state: str | None = None
    estimated_seconds: int | None = Field(default=None, alias="estimatedTimeToMerge")
    total: int | None = Field(
        default=None, validation_alias=AliasPath("mergeQueue", "entries", "totalCount")
    )
    checks: GithubCheckCounts | None = Field(
        default=None,
        validation_alias=AliasPath("headCommit", "statusCheckRollup", "contexts"),
    )


class GithubReviewThread(BaseModel):
    is_resolved: bool = Field(alias="isResolved")
    is_outdated: bool = Field(alias="isOutdated")
    path: str
    line: int | None = None
    body: str = Field(default="", validation_alias=AliasPath("comments", "nodes", 0, "bodyText"))
    url: str | None = Field(
        default=None, validation_alias=AliasPath("comments", "nodes", 0, "url")
    )

    def neutral(self) -> ReviewThread:
        return ReviewThread(
            is_resolved=self.is_resolved,
            is_outdated=self.is_outdated,
            path=self.path,
            line=self.line,
            body=self.body,
            url=self.url,
        )


class GithubCheck(BaseModel):
    name: str
    state: str
    url: str | None = None
    is_required: bool = Field(default=False, alias="isRequired")
    run_id: int | None = Field(default=None, alias="databaseId")
    app_id: int | None = Field(
        default=None, validation_alias=AliasPath("checkSuite", "app", "databaseId")
    )
    workflow_id: int | None = Field(
        default=None,
        validation_alias=AliasPath("checkSuite", "workflowRun", "workflow", "databaseId"),
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_check_run(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {
            **value,
            "state": value.get("conclusion") or value.get("status") or value.get("state"),
        }


def latest_checks(
    checks: tuple[GithubCheck, ...],
) -> tuple[Check, ...]:
    """Drop superseded check runs from a rollup.

    Rollups include superseded runs. Check-run IDs distinguish replacements even before a queued
    job starts; start/completion times can put older runs last. Keep distinct apps/workflows and
    commit statuses independently required.
    """

    latest: dict[tuple[str, int | None, int | None], GithubCheck] = {}
    for check in checks:
        if check.run_id is not None:
            key = (check.name, check.app_id, check.workflow_id)
            prior = latest.get(key)
            if prior is None or check.run_id > (prior.run_id or 0):
                latest[key] = check
    return tuple(
        Check(name=check.name, state=check.state, url=check.url, is_required=check.is_required)
        for check in checks
        if check.run_id is None or latest[(check.name, check.app_id, check.workflow_id)] is check
    )
