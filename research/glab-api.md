# glab api as a transport

Ticket: https://github.com/pylbrecht/jj-stack/issues/3 (map: issue 1, "GitLab support via glab").
Checked: glab `1.128.0 (519ea19f7)`, read at the same commit of
[gitlab-org/cli](https://gitlab.com/gitlab-org/cli/-/tree/519ea19f7d3e9c701a4063f277ae27254bbafdad).
Live checks: read-only `glab-cf api` calls against `git.envelio.sh` (GitLab `19.5.4-ee`, API
host `git.envelio.org` behind Cloudflare Access). The only POSTs sent were read-only GraphQL
queries (and glab's own telemetry event, see "Parallel invocation").

Source links below use `SRC` for
`https://gitlab.com/gitlab-org/cli/-/blob/519ea19f7d3e9c701a4063f277ae27254bbafdad`.

## Verdict

`glab api` can carry every request jj-stack makes today: REST with any method, GraphQL,
arbitrary request headers, raw JSON bodies on stdin, and response status and headers through
`-i`. Nothing found blocks the "glab api only" decision. The transport is lower level than
the httpx client in four ways that the adapter must cover itself:

1. **Exit code is coarse.** Every failure exits 1: HTTP status >= 300, GraphQL top-level
   `errors`, network errors, flag errors, and the wrapper's own failures. To branch on the
   status (404, 409, 422, 429) or read `Retry-After`, pass `-i` and parse the status line and
   headers from stdout.
2. **No retries.** `glab api` sends each request once (no retry on 429 or 5xx). jj-stack keeps
   its own rate-limit and retry policy.
3. **Exit 0 does not prove a GitLab answer.** A response that is not an error status is exit 0
   whatever its content type. A wrong `--hostname` behind Cloudflare Access returned the
   Access login page as `HTTP 200` HTML, exit 0. Check `Content-Type: application/json`.
4. **`--paginate` output is not one JSON document** without `--output ndjson`, and a failure
   mid-way leaves earlier pages on stdout with exit 1. Prefer an own page loop with `-i`.

Set `GLAB_SEND_TELEMETRY=0` and `GLAB_CHECK_UPDATE=false` in the child environment: each call
otherwise POSTs a telemetry event to the GitLab host (about 0.15 s per call measured), and the
daily update check writes a notice to stderr.

## REST and GraphQL

- The endpoint is a path under `/api/v4/`, the literal `graphql` (sent to `/api/graphql`), or
  a full URL containing `://` (used as is). `SRC/internal/commands/api/http.go`
  (`httpRequest`).
- GraphQL: with `-f`/`-F`, `query` and `operationName` go top level and every other field
  becomes a variable (`groupGraphQLVariables`, `http.go`). With `--input -`, the body is sent
  unchanged, so `{"query": ..., "variables": ...}` works. Verified live (exit 0).
- GraphQL errors: for `graphql`, glab always parses the JSON body. A non-empty top-level
  `errors` array makes it print the body to stdout, print the first message as
  `glab: <message>` to stderr, and exit 1, even when the status is 200 and `data` is partly
  present. `processResponse` and `parseErrorResponse` in `SRC/internal/commands/api/api.go`.
  Verified live with an unknown field and with an unused variable.
- Mutation failures that GitLab reports as data (the payload's own `errors: [String]` field)
  are not top-level errors, so glab exits 0. The adapter must read `data.<mutation>.errors`.
  GitLab: "All mutation responses have an `errors` field"
  ([GraphQL style guide](https://docs.gitlab.com/development/api_graphql_styleguide/)).

## Pagination

REST (`SRC/internal/commands/api/pagination.go`):

- `--paginate` is refused for non-GET REST requests (`validate`, `api.go`).
- glab adds `per_page=100` unless `per_page` is already in the path or fields (`addPerPage`).
- It follows the `rel="next"` URL of the `Link` header until there is none (`findNextPage`).
  This covers both offset and keyset pagination, because GitLab puts the keyset cursor in the
  next link ([REST pagination](https://docs.gitlab.com/api/rest/#pagination)).
- Pages are requested one after another, never in parallel.
- Output: each page body is written as received. Default `json` output for 5 pages was
  `[...][...][...][...][...]` with no separator: not one JSON document, although `--help`
  says "Arrays are output as a single JSON array". `--output ndjson` writes one array element
  per line (9 lines for 9 groups). Verified live with `groups?per_page=2&top_level_only=true`.
- With `-i`, every page prints its own header block, so headers and bodies interleave.
- A failing page stops the loop with exit 1; earlier pages are already on stdout.

GraphQL:

- The query must declare `$endCursor: String` and select `pageInfo { hasNextPage endCursor }`.
  glab scans the response for the first `pageInfo` object and sets the `endCursor` variable
  until `hasNextPage` is false (`findEndCursor`). Only one connection per query can page.
- Output is one JSON object per page, concatenated with no separator; `--output ndjson` puts
  each on its own line. Verified live with `currentUser.groups(first: 1)`.

Recommendation: page REST lists in jj-stack with `-i` and the `Link` header, so 429 handling
and error mapping stay per request. `--paginate --output ndjson` is acceptable for small
read-only lists where a whole-call retry is fine.

## Host resolution and `--hostname`

- With `--hostname`, glab uses that host and does not read the Git repo
  (`run`, `api.go`). Without it, glab asks `BaseRepo()`, which runs `git remote -v` and
  `git config` (`SRC/internal/git/git.go`). If that fails, glab silently uses the default
  host (the `host` config key, `GITLAB_HOST`, or `gitlab.com`).
- In a non-colocated jj repo there is no `.git` in the working copy, so glab's remote
  detection fails. jj-stack must derive the host from the jj remote URL itself and always pass
  `--hostname`. It must also not use the `:fullpath`/`:id` placeholders, which need the same
  detection; use the URL-encoded project path instead.
- `--hostname` must be the **hosts key** in glab's config, which normally is the host of the
  Git remote URL (here `git.envelio.sh`). glab maps it to `api_host` (`git.envelio.org`) and
  `api_protocol`, and adds `custom_headers` (here `Cf-Access-Token` from `CF_ACCESS_TOKEN`)
  only for that host (`SRC/internal/api/custom_headers_transport.go`).
- A token from `GITLAB_TOKEN` is sent only to the default host, the `GITLAB_HOST` host, or a
  host already in the config; "A host named only in a command argument receives no token"
  (`SRC/internal/config/schema.go`). Live: `--hostname git.envelio.org` printed a warning to
  stderr, sent no token and no Cloudflare header, and got the Cloudflare Access page as
  `HTTP 200`, exit 0.
- `--hostname` is validated (`glinstance.HostnameValidator`). An unknown DNS name fails with a
  network error, exit 1.

## Output shape

- Body goes to stdout. When stdout is not a terminal, colour is off and the body is copied
  byte for byte (compact JSON as GitLab sends it). The pretty printer only runs with colour
  on. No pager runs when stdout is not a terminal.
- `204 No Content`: no body, exit 0.
- `-i`: status line `HTTP/2.0 404 Not Found\n`, then sorted headers each ending `\r\n`, then
  an empty `\r\n` line, then the body, all on stdout (`processResponse`, `printHeaders`).
  This holds on error responses too. Verified live.
- `--silent` drops the body but not the `-i` headers.

## Exit codes and error output

From `SRC/cmd/glab/main.go`, `SRC/internal/cmdutils/errors.go`, `processResponse` (`api.go`):

- `0`: status 1xx/2xx, and for GraphQL no top-level `errors`.
- `1`: status > 299. If the body is JSON, glab prints it to stdout and prints `glab: <message>
  (HTTP <code>)` (from `message` or `errors`) to stderr; otherwise it prints the raw body to
  stdout and `glab: HTTP <code>` to stderr. Live 404: stdout
  `{"message":"404 Project Not Found"}`, stderr `glab: 404 Project Not Found (HTTP 404)`.
- `1`: transport errors (DNS, TLS, refused) and flag errors, with a formatted message on
  stderr.
- `1`: refused redirect that would change the method (`SRC/internal/api/redirect.go`).
- `2`: glab cannot read its config.
- Rate limits: GitLab answers `429` with the plain-text body "Retry later" and, for
  Rack::Attack limits, `RateLimit-*`, `RateLimit-ResetTime`, and `Retry-After` headers;
  application limits return 429 without them
  ([user and IP rate limits](https://docs.gitlab.com/administration/settings/user_and_ip_rate_limits/)).
  glab treats 429 like any other status > 299: exit 1, `glab: HTTP 429`, no retry. The
  headers are only visible with `-i`. On `git.envelio.org` no `RateLimit-*` headers were
  present on normal responses, so these limits seem to be off there.
- The `glab-cf` wrapper adds its own exit-1 failure (`glab-cf: Cloudflare login needed ...`
  on stderr) when no Cloudflare token is available and there is no terminal. With `-i`, the
  adapter can tell this from an HTTP failure because stdout has no `HTTP/` status line.

## Request bodies

- `-f key=value`: string. `-F key=value`: `true`/`false`/`null`/integers become JSON types,
  values starting with `[` or `{` are parsed as JSON, and a value starting with `@` is **read
  from a file** (or stdin for `@-`). Bracketed names are errors in JSON bodies.
- Any field without `-X` makes the request a POST. For GET and DELETE, fields become query
  parameters.
- `--input -` reads the complete body from stdin and sends it as
  `application/json; charset=utf-8`; fields then become query parameters. `--input` and
  `--paginate` are mutually exclusive.
- Recommendation: send every REST and GraphQL write as `--input -` with a JSON body that
  jj-stack serialises. This avoids `-F` type guessing (a title `true` or `@notes`), argv
  length limits, and secrets in the process list. Pass query parameters in the path.
- `-H 'Name: value'` adds headers, so conditional headers (for example `If-Match`) can be sent
  where a GitLab endpoint supports them.

## Parallel invocation

- Cost per call, measured through `glab-cf` on this machine: about 0.45 s with defaults,
  about 0.30 s with `GLAB_SEND_TELEMETRY=0`. The wrapper (fish, `cloudflared access token`,
  Keychain read) costs about 0.1 s of that. Each call is a new process with a new TLS
  connection; there is no connection reuse between calls. 8 parallel calls took 0.82 s in
  total.
- Telemetry: by default every command POSTs `/api/v4/usage_data/track_event` to the GitLab
  host after the request, bounded at 2 s (`SRC/cmd/glab/telemetry.go`). Seen live with
  `GLAB_DEBUG_HTTP=1`.
- Shared state is safe: glab writes its config only through a `flock` on `config.lock` and an
  atomic rename (`SRC/internal/config/flock.go`, `SRC/internal/config/writefile.go`). Writes
  happen for the daily update-check timestamp and one-time migrations, not per request.
- Update check: at most every 24 h, after a successful command, glab queries gitlab.com and
  may print an "update available" notice to stderr (`SRC/internal/commands/update/
  check_update.go`). `GLAB_CHECK_UPDATE=false` turns it off.
- Wrapper under parallel load: when the cached Cloudflare token or the 5-day Keychain token
  cache has expired, every parallel call tries to refresh at once (several 1Password prompts,
  or all fail without a terminal). Run one cheap call (for example `GET version`) before a
  parallel batch.
- `GLAB_DEBUG_HTTP=1` redacts `PRIVATE-TOKEN` and `Authorization` but not `Cf-Access-Token`
  (`SRC/internal/api/debug_transport.go`); do not enable it by default.

## What a wrapper must pass through

`glab-cf` (`~/.dotfiles/fish/.config/fish/functions/glab-cf.fish`) sets `CF_ACCESS_TOKEN`,
`GLAB_CONFIG_DIR`, and `GITLAB_TOKEN`, then runs `glab $argv`. It passes through, and any
configured command must pass through unchanged:

- all arguments, in order, without re-splitting (fish `$argv` keeps each argument whole);
- stdin (needed for `--input -`);
- stdout and stderr, separately (stdout carries `-i` headers and the body, stderr carries the
  `glab:` message);
- glab's exit status;
- the caller's environment, so `GLAB_SEND_TELEMETRY`, `GLAB_CHECK_UPDATE`, and `NO_COLOR`
  reach glab.

`glab-cf` reads a leading `--ship` or `--babysit` to pick a token with write scope. So the
configured command must be an argv list (for example `["glab-cf", "--ship"]`), not a single
executable name, and jj-stack must put `api` and its own flags after it.
