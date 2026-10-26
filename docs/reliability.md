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
