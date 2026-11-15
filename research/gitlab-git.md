# GitLab Git-level behaviour (self-managed, Gitaly)

Research for [#5](https://github.com/pylbrecht/jj-stack/issues/5), part of the GitLab support
effort ([#1](https://github.com/pylbrecht/jj-stack/issues/1)).

Question: which Git-level behaviours that jj-stack relies on with GitHub also hold on a
self-managed GitLab instance, where Gitaly serves Git?

Sources are the GitLab docs and the `master` source of
[Gitaly](https://gitlab.com/gitlab-org/gitaly) and
[GitLab Rails](https://gitlab.com/gitlab-org/gitlab), read on 2026-11-15. Source links point
at `master`, so line numbers can drift.

## Summary

- Every Git mechanism that jj-stack uses works on GitLab without change: fetch by object ID,
  `--atomic`, and one `--force-with-lease` per ref.
- Gitaly forces `uploadpack.allowAnySHA1InWant=true`, so you can fetch any object that is in
  the repository by its full ID, also when no visible ref reaches it.
- GitLab keeps each MR version's head and start commits alive with hidden
  `refs/keep-around/<sha>` refs. Thus old submitted commits stay fetchable.
- `refs/merge-requests/<iid>/head` resembles `refs/pull/<n>/head`, but GitLab deletes it 14
  days after the MR closes or merges. GitHub keeps `refs/pull/<n>/head`. jj-stack does not use
  `refs/pull/*` today, so this breaks nothing.
- Server policy can reject an atomic push: protected branches, push rules, secret push
  protection, and custom server hooks. With `--atomic`, one rejected ref aborts all of them.
- GitLab limits the pipelines that one push can start (default 4). A stack of more than four
  branches in one atomic push can leave some branches or MRs without a pipeline. This is the
  one difference that changes a user-visible result.

## What jj-stack relies on

From `src/jj_stack/jj/client.py`:

- `fetch_commits` and `read_remote_git_commit` run
  `git fetch --no-tags --no-write-fetch-head <url> <sha>...`. The SHAs come from PR heads,
  the trunk tip, and submitted commits from tracking (`src/jj_stack/stack/pr_rewrites.py`). A
  submitted commit can be one that a later force-push made unreachable from every branch.
- `import_remote_pr_branch_ref` fetches `refs/heads/<branch>` into a temporary ref, then checks
  the expected target.
- `mutate_remote_pr_branch_refs` runs
  `git push --atomic --no-follow-tags --no-verify --force-with-lease=<ref>:<expected>... <tmp-remote> <sha>:<ref>...`
  with one lease per PR branch. An empty expected value means "the ref must not exist".

`src/jj_stack/github/pr_refs.py` only parses PR numbers and URLs and checks the PR head
branch name. Nothing in `src/` fetches `refs/pull/*`.

## Fetch of a commit by SHA

**Holds.** Gitaly starts every `git upload-pack` with these settings
([`internal/git/gitcmd/command_description.go`](https://gitlab.com/gitlab-org/gitaly/-/blob/master/internal/git/gitcmd/command_description.go)):

```go
ConfigPair{Key: "uploadpack.allowFilter", Value: "true"},
// Enables the capability to request individual SHA1's from the
// remote repo.
ConfigPair{Key: "uploadpack.allowAnySHA1InWant", Value: "true"},
```

- `allowAnySHA1InWant` is the most permissive mode: the server accepts a `want` for any object
  that it has, reachable or not
  ([git-config `uploadpack.allowAnySHA1InWant`](https://git-scm.com/docs/git-config#Documentation/git-config.txt-uploadpackallowAnySHA1InWant)).
  Gitaly's own comment confirms this: with the setting, "not our ref" means that the object
  does not exist, not that it is unreachable
  ([`internal/git/gitcmd/upload_pack.go`](https://gitlab.com/gitlab-org/gitaly/-/blob/master/internal/git/gitcmd/upload_pack.go)).
- The setting applies in protocol v0, v1, and v2. Protocol v2 is on by default for HTTP. For
  SSH through OpenSSH, an admin must add `AcceptEnv GIT_PROTOCOL`. `gitlab-sshd`, Helm, and
  Docker installs need no change
  ([Git protocol v2](https://docs.gitlab.com/administration/git_protocol/)). jj-stack does not
  depend on the protocol version.
- Hidden refs do not limit this. Gitaly hides `refs/keep-around/`, `refs/remotes/`, and
  `refs/tmp/` from the advertisement
  ([`internal/git/reference.go`](https://gitlab.com/gitlab-org/gitaly/-/blob/master/internal/git/reference.go)),
  but you can still fetch their objects by ID.

**How long an unreachable commit stays.** The fetch works only while the object exists:

- GitLab writes `refs/keep-around/<sha>` for the `head_commit_sha` and `start_commit_sha` of
  every MR diff version (`enqueue_keep_around_commits` in
  [`app/models/merge_request_diff.rb`](https://gitlab.com/gitlab-org/gitlab/-/blob/master/app/models/merge_request_diff.rb)),
  and for the merge commit (`keep_around_commit` in
  [`app/models/merge_request.rb`](https://gitlab.com/gitlab-org/gitlab/-/blob/master/app/models/merge_request.rb)).
  The write is asynchronous (`MergeRequests::KeepAroundRefsWorker`). Each push to an open MR's
  source branch creates a new diff version, so a submitted commit that GitLab saw as an MR head
  stays fetchable after a force-push.
- An object with no ref at all is pruned by housekeeping after a 10-day grace period
  (`StaleObjectsGracePeriod` in
  [`internal/git/stats/repository_info.go`](https://gitlab.com/gitlab-org/gitaly/-/blob/master/internal/git/stats/repository_info.go)),
  or after 30 minutes when an admin runs the `PruneUnreachableObjects` RPC
  ([`prune_unreachable_objects.go`](https://gitlab.com/gitlab-org/gitaly/-/blob/master/internal/gitaly/service/repository/prune_unreachable_objects.go)).
  Such a commit is one that was pushed and force-pushed away before GitLab recorded an MR
  version for it, or one on a branch with no MR.

GitHub also serves unreachable objects by ID, so the current assumption holds. The remaining
risk is the same on both: a commit that no ref ever kept alive can be garbage-collected.

## `refs/merge-requests/<iid>/*` compared with `refs/pull/<n>/*`

- Refs: `head`, `merge`, `train`, and `rebase_on_merge` under
  `refs/merge-requests/<iid>/` (`ref_path`, `merge_ref_path`, `train_ref_path`,
  `rebase_on_merge_path` in `app/models/merge_request.rb`). `<iid>` is the project-scoped MR
  number, the number shown in the UI.
- `head` is written in the target project by `fetch_ref!`, which copies the source branch tip,
  also from a fork. GitLab calls it when it creates an MR diff version, so at MR creation and
  after each push. The update runs after the push, through the post-receive refresh, so it lags
  the branch for a short time.
- `merge` is a temporary merge commit that GitLab writes for mergeability checks and merged
  results pipelines. `train` exists only for merge trains. GitHub's `refs/pull/<n>/merge` is the
  closest match.
- Visibility: Gitaly marks `refs/merge-requests/` as read-only. They appear in `ls-remote` and
  you can fetch them, but `receive.hideRefs` makes every push to them fail
  ([`reference.go`](https://gitlab.com/gitlab-org/gitaly/-/blob/master/internal/git/reference.go),
  `hiddenReceivePackRefPrefixes` in `command_description.go`). GitHub also rejects pushes to
  `refs/pull/*`.
- Fetch: `git fetch <remote> merge-requests/<iid>/head` or the refspec
  `+refs/merge-requests/*/head:refs/remotes/origin/merge-requests/*`
  ([MR troubleshooting](https://docs.gitlab.com/user/project/merge_requests/merge_request_troubleshooting/)).
- **Lifetime: the difference.** Close and merge schedule `MergeRequests::CleanupRefsService`
  with `TIME_THRESHOLD = 14.days`
  ([`cleanup_refs_service.rb`](https://gitlab.com/gitlab-org/gitlab/-/blob/master/app/services/merge_requests/cleanup_refs_service.rb),
  [`removes_refs.rb`](https://gitlab.com/gitlab-org/gitlab/-/blob/master/app/services/concerns/merge_requests/removes_refs.rb)).
  After 14 days a cron worker deletes `head`, `merge`, `train`, and `rebase_on_merge`. It caches
  the merge ref SHA in the database first. The docs state: "The merge request `head` ref is
  deleted 14 days after a merge request is closed or merged", and reopening does not restore
  it. GitHub keeps `refs/pull/<n>/head` for the life of the PR. The commits stay fetchable by
  SHA through keep-around refs.

Consequence: a later GitLab feature that checks out a closed or merged MR whose source branch
was deleted cannot rely on `refs/merge-requests/<iid>/head`. It should fetch the head SHA from
the API by ID instead, which works.

## `git push --atomic`

**Holds.** Gitaly runs stock `git receive-pack`. It sets `receive.advertisePushOptions=true`
and does not change `receive.advertiseAtomic`, which defaults to true
([`command_description.go`](https://gitlab.com/gitlab-org/gitaly/-/blob/master/internal/git/gitcmd/command_description.go);
[git-config `receive.advertiseAtomic`](https://git-scm.com/docs/git-config#Documentation/git-config.txt-receiveadvertiseAtomic)).

Gitaly's pre-receive hook sends all ref changes of the push in one call to GitLab's
`/internal/allowed` check, then runs custom pre-receive hooks with the same list
([`internal/gitaly/hook/prereceive.go`](https://gitlab.com/gitlab-org/gitaly/-/blob/master/internal/gitaly/hook/prereceive.go)).
A pre-receive rejection fails the whole push, also without `--atomic`. With Praefect, the
primary stops the reference transaction for all replicas when the hook fails.

## `--force-with-lease` across several refs

**Holds.** The lease is a client feature: Git compares each lease with the advertised ref and
sends the expected value as the old object ID of each update command. `receive-pack` then
updates the refs in one transaction, which fails when a ref moved
([git-push `--force-with-lease`](https://git-scm.com/docs/git-push#Documentation/git-push.txt---force-with-leaseltrefnamegtltexpectgt)).
Gitaly does not change this. With `--atomic`, one stale lease rejects every ref. An empty
expected value (create only) also works, because the server rejects an update whose old ID is
zero when the ref exists.

## Server policy that can change the result

Each of these rejects through pre-receive or update checks. Git prints the reason as
`remote: GitLab: ...` lines. With `--atomic`, no ref changes.

- **Protected branches.** Wildcard rules such as `*` or `stack/*` can match jj-stack's PR
  branches. GitLab then rejects a force push unless "Allowed to force push" is on, rejects
  deletion through Git (deletion only through UI or API), and can reject creation
  ([protected branches](https://docs.gitlab.com/user/project/repository/branches/protected/);
  messages in
  [`lib/gitlab/checks/branch_check.rb`](https://gitlab.com/gitlab-org/gitlab/-/blob/master/lib/gitlab/checks/branch_check.rb)).
  The default branch is protected by default.
- **Branch names.** GitLab always rejects a new branch whose name starts with a full SHA-1 or
  SHA-256 hex string, and rejects names that fail its ref validator (`branch_check.rb`).
- **Push rules (Premium and Ultimate).** Branch name regex, commit message regexes, unsigned
  commits, commit author must be a GitLab user, author email regex, inconsistent user name,
  prohibited file names, maximum file size, DCO
  ([push rules](https://docs.gitlab.com/user/project/repository/push_rules/)). These check the
  user's commits and branch names, so they can reject a stack that GitHub accepts.
- **Secret push protection (Ultimate)** can reject commits. The push option
  `secret_push_protection.skip_all` skips it.
- **Custom server hooks** that an admin installs run after GitLab's checks with the same ref
  list (`prereceive.go`).
- **Internal refs.** Pushes to `refs/merge-requests/`, `refs/keep-around/`, `refs/pipelines/`,
  `refs/environments/`, `refs/remotes/`, and `refs/tmp/` are refused (`reference.go`).
  jj-stack only pushes `refs/heads/*`.

**Push options.** GitLab advertises push options and supports `ci.skip`, `ci.no_pipeline`,
`ci.variable`, `ci.input`, `integrations.skip_ci`, and `merge_request.*` options such as
`create`, `target`, `title`, `description`, `draft`, `label`, `auto_merge`
([push options](https://docs.gitlab.com/topics/git/commit/)). jj-stack sends none today. The
docs do not say how `merge_request.*` options apply when one push updates several branches.

**Limits per push.** These do not change refs, but change what happens after the push:

- Pipelines: by default one push starts at most 4 branch or tag pipelines. The docs say merge
  request pipelines are also limited
  ([CI/CD limits, "Number of pipelines per Git push"](https://docs.gitlab.com/administration/cicd/limits/)).
  An atomic push of a stack with more than four branches can leave the upper branches or MRs
  without a pipeline. An admin can raise the limit.
- Webhooks and activity: when a push changes more branches than a set limit, GitLab does not
  run push hooks and records one bulk push event
  ([instance limits](https://docs.gitlab.com/administration/instance_limits/)).
- After the push, GitLab refreshes MRs asynchronously. Until the refresh runs, the API can
  still report the old MR head SHA, and `refs/merge-requests/<iid>/head` can lag.

## Not verified

A read-only check against the user's instance (`git.envelio.sh`) was not possible: the SSH
agent refused to sign without interactive approval. No claim above depends on that instance.
Push rules, protected branch patterns, the pipeline limit, and custom hooks on that instance
are unknown.

## Breaks of current assumptions

- None at the Git level. Fetch by SHA, `--atomic`, and per-ref leases behave as on GitHub.
- Possible user-visible difference: the pipeline-per-push limit (default 4) can skip CI for part
  of a large stack pushed in one atomic push.
- `refs/merge-requests/<iid>/head` disappears 14 days after close or merge. jj-stack does not
  use PR refs today, so this matters only if a future design adds them.
