# Site meter netting — operator attestation

**Date:** 2026-08-25 (evening), site-local
**Operator statement (verbatim):** "I do have net metering, no need to prove it, it just is."
**Recorded by:** the coordinator, at the operator's instruction, as the commissioning
evidence for `evening_load_sharing.act_netting_evidence` (DESIGN_EVENING_LOAD_SHARING
v1.1 §11/§12, amendment E6).

## What this attests

The site's polyphase billing meter nets energy across the three phases: import on
one register offsets export on another within the billing interval, so power
delivered by a pod on one phase serves load on another without a paired purchase.
This is the physics fact the evening load-sharing adviser's economics stand on
(on a gross/per-phase-billed meter, sharing would sell at the feed-in rate what
it displaces at the import rate — ~28.77 ¢/kWh against the site's 30.77 ¢ import
/ 2.0 ¢ feed-in).

## Corroboration already on record

- The operator's bill (commissioned 2026-08-24) shows a single general-usage
  import line, a night-window import line, and a single export credit line
  (524.12 kWh at 2.0 ¢) — the register structure of a netting meter, not
  per-phase gross billing.
- The workstream's standing `NET_BILLED` acknowledgement (recorded in the
  2026-08-25 calibration research round, used by the cycling economics).

The operator's attestation is the authoritative fact; the bill structure and the
standing acknowledgement corroborate it. No further verification is required.
