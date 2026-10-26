# Dashboard reliability update

Development starts at `a988fffaa840bad303b1d3cd08b6a4f6d3b61692`. No deployment,
image rebuild, GPU work, production state or Home Assistant changes are performed.

## Daily quotes

`daily-quotes.json` and `daily-quotes.lock` live in the existing private
`GPU_RUNNER_CLIENT_STATE` mount (`/var/lib/dashboard-gpu-client`). AtomicStore and
NativeLock provide durable replacement and cross-process exclusion. The ledger
stores up to ten successful day/quote pairs and one pending exact runner identity,
target and payload. Prompt snapshots stay private; do not expose or delete them
as a workaround for a retained owner. Keep this mount across container recreation.

Toronto's calendar day is explicit, independent of the container timezone. Normal
renders and `/todo_ai_refresh` share this ledger, with one validated successful
quote per day. A quote must be complete (`done: true`, no length stop), usable,
ASCII and a single line. No truncation or character deletion launders an invalid
answer into success. Temporary fallbacks are never successful quote history.

A render makes one intentional attempt if eligible. Invalid successful/released
output or KnownJobFailure clears eligibility for the next intentional render.
Any other submission uncertainty preserves the intent. Later renders only GET
that exact job and, if successful and released, its result. Failed/released jobs
allow a subsequent intentional attempt; missing/unreleased/mismatched jobs stay
blocked, including across day rollover. No automatic POST replay or GPU bypass.

Task formatting remains separate. Cached task rows contain no embedded daily
quote, so regenerating wallpaper cannot invalidate today's quote. With at most
three occupied task rows, line 4 stays a buffer and line 5 contains the quote.
Four or more task rows keep their space. The recovery indicator is unchanged.

## Exact recovery acknowledgment

Dependency: separately reviewed runner branch
`codex/read-only-recovery-incidents-20261026`, commit `67fc317`, directly on
`f2b9f5e`. Its authenticated GET `/recovery-incidents` exposes opaque incident IDs,
first observation dates and resolution aliases. No mutation, probe, GPU, admission,
cleanup or recovery behavior changes. GET `/recovery-summary` remains unchanged.

The dashboard compares current in-window incident aliases with acknowledged
aliases. It never subtracts aggregate counts. Aliases preserve acknowledgment
across later reboot grouping. Old incidents expire using their first observation
date; new incidents count even if the aggregate total happens to stay unchanged.
`operator_required` remains independent and visible, including on stale status.

`POST /recovery-counter/acknowledge` requires `Authorization: Bearer TOKEN` with a
dedicated `RECOVERY_ACK_TOKEN` in private configuration (43-128 URL-safe characters;
generate with `secrets.token_urlsafe(32)`). Do not reuse `GPU_RUNNER_TOKEN`. No
credential is created, printed, logged or committed by this update. Comparison
uses `hmac.compare_digest`. State is atomic `recovery-ack.json` under the existing
private mount with a native write lock. Acknowledgments union previous aliases;
capacity exhaustion fails explicitly instead of silently forgetting incidents.

The endpoint only fetches a fresh read-only inventory and records local display
state. It never renders, uploads or starts GPU work. The next normal render reads
the acknowledgment in the existing bounded background worker. Without the runner
enhancement, acknowledgment returns sanitized HTTP 503. Before any acknowledgment,
legacy aggregate display still works; after acknowledgment, missing exact inventory
displays unknown/stale rather than an approximate number. Raw aggregate diagnostics
remain in the runner endpoint and local `recovery-summary.json` cache.

Later Home Assistant configuration example (not installed):

```yaml
rest_command:
  dashboard_acknowledge_recoveries:
    url: "http://192.168.0.102:8787/recovery-counter/acknowledge"
    method: POST
    headers:
      authorization: !secret dashboard_recovery_ack_authorization
    timeout: 10
```

In private `secrets.yaml`, configure `dashboard_recovery_ack_authorization` once
as the full `Bearer ...` value matching Media's private `RECOVERY_ACK_TOKEN`.
Invoke `rest_command.dashboard_acknowledge_recoveries` from an operator button or
script with `response_variable: acknowledgment`; inspect `acknowledgment.status`
and its JSON `content`. HTTP 401 means authentication failed; 503 means private
configuration, fresh inventory or durable state could not be verified. Never put
the token in action data, URLs, notification text or routinely entered fields.
