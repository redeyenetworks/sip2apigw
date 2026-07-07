# Document Control

## About this document

This is the official product manual for the **RedEye sip2api Gateway** (Code Blue / RRT Notification Gateway). It is a **single, shared deliverable**: the customer (Tift Regional Medical Center) and the RedEye Network Solutions support team receive the **same** document, in full — no separate "internal" edition, nothing withheld between the parties.

| Field | Value |
|---|---|
| Product | RedEye sip2api Gateway |
| Product build described | **v1.6.5+ (build `c23f3eb`)** — the in-progress v1.7 line, current production |
| Document version | **2.0** |
| Date | {{DATE}} |
| Prepared by | RedEye Network Solutions LLC, in conjunction with Claude Code |
| Prepared for | Tift Regional Medical Center **and** RedEye Support (shared) |
| Classification | Confidential — shared between RedEye and the named customer |

## Scope & version note

This edition documents the product **as currently deployed in production** — build `c23f3eb` (branch `main`), which is **v1.6.5 plus 6 commits** on the unreleased **v1.7** line. The As-Built section is a live inventory of the production host captured **2026-07-07**. Where features are still in development (High-Availability, zero-downtime restarts, remaining Dashboard-v2 and dedupe/logging items), they appear only in the **High-Availability Plan** and **Roadmap** sections, clearly marked as *planned*.

## Regeneration & cadence

This manual is produced by a repeatable, in-repository documentation pipeline (the `product-docs` skill) and is **regenerated on each release**. Each regeneration refreshes the modular source sections and the live host As-Built inventory, recompiles the `.md` and `.docx`, and appends a row to the revision history below. The compiled deliverable is published as a **versioned release asset**.

## Revision history

| Doc ver. | Product build | Date | Author | Summary of changes |
|---|---|---|---|---|
| 1.0 | v1.5.1 | 2026-07-02 | RedEye + Claude Code | Initial edition — documented the v1.5.1 single-service deployment. |
| **2.0** | **v1.6.5+ (`c23f3eb`)** | {{DATE}} | RedEye + Claude Code | Regenerated against the current production build: two-service topology, durable delivery (outbox + retries + escalation), watchdog, real health + inbound-liveness, enforcing dedupe, RFC3339/UTC timestamps, Dashboard v2. Reliability content moved from "roadmap" to "current"; roadmap re-scoped to the open items (HA, zero-downtime restarts, etc.). As-Built refreshed from a live host inventory. |

## A note on safety-critical use

The RedEye sip2api Gateway participates in a **life-safety** notification path (Code Blue / RRT overhead paging). Operators and support staff should read the **Reliability, Delivery Guarantees & Known Limitations** section in full and understand the governing principle: *a duplicate overhead page is acceptable; a missed page is never.*
