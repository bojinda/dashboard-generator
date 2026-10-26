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
`codex/read-only-recovery-incidents-20261026`, commit `923e2a8`, directly on
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

These examples follow Home Assistant's documented
[RESTful Command response/header support](https://www.home-assistant.io/integrations/rest_command/)
and [private secret references](https://www.home-assistant.io/docs/configuration/secrets/).

## Pipeline results and later notifications

Legacy `ok` indicates the API/output operation completed. It is not a claim that
every stage succeeded. Completed render responses and `/render_status.last` add:

- `pipeline_status`: `success`, `degraded`, `failed` or `uncertain` (and `running`
  while an operation is active).
- `fully_successful`, `wallpaper_generated`, bounded per-stage `stages` and
  structured `warnings` with fixed reasons and optional safe runner error codes.

Stages distinguish weather/season/task/quote AI, actual wallpaper generation,
composition, conversion and delivery. A successful composed PNG containing error
imagery has `wallpaper_generated: false` and a degraded pipeline. A temporary quote
or deterministic prompt fallback also cannot become full success. Known released
runner failures are distinguished from invalid output and retained/uncertain jobs.
Uncertainty aborts subsequent GPU stages without retries; CPU-only work may finish
after a quote returns its already-quarantined temporary fallback.

Conversion must validate the byte count before delivery. Soft EPD failure keeps
the completed PNG/bin and legacy `epd_push: failed_soft`, but persists a structured
delivery failure and `pipeline_status: degraded`. Hard delivery failure retains
successful composition/conversion evidence and finishes as failed. Async and sync
renders, plus GPU-free existing-image conversion paths, use the same delivery code.
No exception message, private model output, prompt, credential, URL or stderr is
copied to these public diagnostics; fixed codes replace former raw EPD errors.

The private OutputSlot operation/active records retain the sanitized pipeline in
`details.pipeline`, atomically via the existing store. The underlying completed
record keeps `status: ok` for request-ID reconnect compatibility, even when the
additive pipeline status is degraded. Never equate that legacy field with full
pipeline success. Old/interrupted IDs still cannot replay GPU work. Status reads
support `GET /render_status?request_id=ID` to avoid confusing concurrent triggers;
the async accepted reply now includes its durable request ID. Preserve the state
mount and the original X-Request-ID on transport uncertainty; do not make an
automatic new render request to try to repair a retained GPU owner.

Later notification workflow (documentation only): keep the existing render action,
assign one unique X-Request-ID to each intentional render, save the accepted reply
with `response_variable`, and poll the matching status until `running: false`.
Notify on `degraded`, `failed`, `uncertain`, timeout, or a missing status record.
Do not post another render on timeout. For example, add a status-only command:

```yaml
rest_command:
  dashboard_render_status:
    url: "http://192.168.0.102:8787/render_status?request_id={{ request_id }}"
    method: GET
    timeout: 10
```

After the existing completion wait, an operator can later add actions like:

```yaml
- action: rest_command.dashboard_render_status
  data:
    request_id: "{{ dashboard_request_id }}"
  response_variable: dashboard_status
- if: >-
    {{ dashboard_status.status != 200 or
       dashboard_status.content.last.pipeline_status | default('unknown') != 'success' }}
  then:
    - action: persistent_notification.create
      data:
        title: "Dashboard pipeline needs review"
        message: >-
          Request {{ dashboard_request_id }}: inspect the structured stage
          status and warning codes in this automation trace.
```

## Minimal later deployment sequence (not executed)

1. Review the three dashboard commits and the independent runner read-only commit.
   Keep Phase B inactive. Arrange a separately approved maintenance window with
   callers excluded and ownership resolved through its existing approved process.
2. Capture the installed package/config, app source, private state and mount/image
   bindings for rollback. Do not replace or reset journals, plans, operation IDs,
   quote history, acknowledgment state or private credentials. Confirm the exact
   workflow bytes/CRLF and `dashboard-gen-runner:r2-20261009` image pin.
3. After separate authorization, install only the read-only runner enhancement
   over `f2b9f5e`; verify existing aggregate/auth behavior plus the new inventory
   without GPU work. No policy, ownership or admission migration is required.
4. Install the reviewed dashboard Python files through its verified existing app
   bind mount, preserving the private state mount and pinned image. Do not rebuild
   the image. Configure only the new dedicated acknowledgment secret privately.
   If actual mount/runtime bindings differ, stop and revise the installation plan.
   For first activation, use a fresh Toronto day with callers excluded across the
   boundary, or separately verify and migrate an existing successful daily quote.
   The legacy process-only cache cannot distinguish historical fallback content
   reliably, so do not blindly seed it as a validated quote or regenerate a known
   successful same-day quote during migration.
5. A separately approved dashboard restart then enables the new source. Verify
   health, status/auth rejection, restored private state and exact inventory
   connectivity without generation. Acknowledgment itself changes display state;
   exercise it only deliberately. Any later render/hardware test needs its own
   authorization. Home Assistant configuration remains a separate reviewed change.
6. Roll back application/package source using the captured versions and same image
   and mounts. Preserve new durable records and opaque acknowledgment state. Do
   not downgrade the runner while owners are unresolved, or delete a pending quote
   intent to force another job. Keep callers excluded until deliberately restored.
