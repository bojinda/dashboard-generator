# Bounded Media dispatch reconciliation

This change affects only `app/media_dispatch.py`. Runner, recovery/E-stop, SDKs,
HA, Music and the four ToDo implementation files are unchanged.

The former 0.6-second GET cap rejects ordinary valid replies observed around
0.9–1.3 seconds. Increasing that cap alone is insufficient: the three-second
whole-pass deadline includes readiness, historical jobs, 404 proofs and final
ownership validation.

The request cap is now **2.5 seconds** and the **whole pass is nine seconds**.
The existing transport applies the smaller of these absolute deadlines, including
response headers/body; there is no retry or unlimited wait. The render-admission
and render-status HA callers allow ten seconds, leaving a one-second margin for
local response handling. Recovery-card transport allows a longer bound and does
not constrain this smaller budget. ToDo's existing 150-second caller is unchanged.

The observed history shape has 394 operations / 313 references: five currently
unsettled references have positive bound receipts, one requires fresh double-404
absence validation, and one requires a terminal/released native status. The full
pass needs **three resource GETs and three job GETs**. With resource replies at
1.333 seconds and job replies at 0.924 seconds, that is 6.771 seconds before local
work. Tests reconstruct the counts and proof classes using generic identities,
not captured journals or private payloads.

All F06 checks remain: originals are hashed, receipt bindings validated, current
owners checked before/after history, exact job identity/release verified, and
404 absence revalidated twice with readiness/age/ownership proof on every pass.
Positive receipts permit bounded progress on a later pass. The eight-query cap
is unchanged. Total-budget exhaustion reports `output_reconciliation_bound_exceeded`;
a request failure before that deadline still reports `runner_unavailable`.

Nine seconds cannot accommodate every possible chain of valid slow replies.
For example, even a single 404 path needs five GETs, so five two-second replies
cannot fit beneath the ten-second caller plus response margin. Such a pass must
remain blocked; this fix does not skip a proof or trust a historical 404 to make
it fit. This is a bounded correction for the measured path, not a guarantee of
unbounded reconciliation or unattended production acceptance.

`tests/test_dispatch_timeouts.py` exercises real localhost HTTP delays, the real
`recovery_media:app` entrypoint via Uvicorn with a ten-second HTTP caller, all
initial/intermediate/final readiness reads, durable progress/restart at the total
deadline, 25 delayed positive references and genuine request timeouts. Existing
dispatch-progress tests retain the 5,000-plus records, tampering, ownership and
404 regressions; ToDo/quote/cache tests are unchanged. GPU observations/jobs are
synthetic and no inference is run. Run `python -m unittest discover -s tests` in
the dependency-correct environment; use `tests.test_dispatch_timeouts` for the
focused cases when the repository root and tests helpers are importable.

Deploy only the Git blob for this one app file after separate approval. Back up
its original bytes/metadata and preserve receipts, caches, pending intents and
protected ToDo files. A future status read invokes supported CPU reconciliation
and can append receipts; it must be included explicitly in installation readback
authorization. No manual render/ToDo request, recovery or E-stop is needed to
install or test this correction offline.
