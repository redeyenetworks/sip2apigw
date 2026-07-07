# Executive Summary

The **RedEye sip2api Gateway** is a life-safety integration appliance that turns a nurse-call event into an overhead page — automatically, and reliably enough to trust with a Code Blue. When a clinician at Tift Regional Medical Center pulls a Code Blue or presses a Rapid Response button, the Rauland nurse-call system emits a **SIP INVITE**. The gateway answers that call, reads who and where from the SIP headers, composes a spoken announcement, and fires an **InformaCast Fusion** scenario that broadcasts it to the overhead IP speakers. No human retypes a room number, and no page waits on someone to notice a screen.

> **SIP in. Page out. Every time.**

## Why this product exists

Overhead notification for a Code Blue or RRT has one job and no acceptable failure mode: **a real page must never be dropped, duplicated, delayed, or sent to the wrong place.** The Rauland nurse-call system and InformaCast Fusion do not speak a common language — one talks SIP, the other a REST API behind OAuth2. Before this gateway, bridging them meant a fragile, best-effort hop with no memory: if the single outbound call to Fusion timed out, the page was simply gone, with nothing to retry it and no one alerted that it had vanished. That is exactly what happened on **2026-06-12**, when a Code Blue was lost to a momentary network timeout during an inline token fetch. That incident is the reason the current release exists, and it is the class of failure the current release is built to make impossible.

## What it does

The gateway sits on host `sip2apibridge` on the clinical network and runs continuously as two independent services so that the paging path is never at the mercy of the reporting UI:

- **The call-path service** (`sipgw.service`) receives every SIP INVITE, validates its source against an IP allowlist, parses the caller's area, room, and bed, resolves them to human-readable names, assembles the text-to-speech announcement, and drives its **durable delivery** to Fusion.
- **The dashboard service** (`sipgw-dashboard.service`) is a separate, read-only web UI on port 8080. It shows the live call history, a 90-day chart of calls by type, per-call detail correlated across every log stream, a date-picker log viewer, and a real health signal — and it can be restarted at any time **without interrupting a single page.**

A typical announcement, for a Code Blue in Room 201 of the E.D., is spoken to every configured speaker as: *"Attention! Attention! Code Blue! 1st Floor. E.D. Room 201."* — repeated three times.

## The reliability principle

The design rule behind every feature is **record-first, then deliver.** The instant a call is answered, the page is written to a durable, crash-safe database **before any attempt is made to send it.** Only then does a background delivery worker take over, sending the page to Fusion with **bounded retries and exponential backoff**, surviving a Fusion outage or even a process crash mid-send. If a page still cannot be delivered, it is **escalated to a human channel** rather than silently lost. The OAuth token that authorizes each send is refreshed in the background, off the critical path, so a real page never waits on a token round-trip — closing the exact gap that lost the 2026-06-12 Code Blue.

Two further guarantees round out the current production build:

- **Duplicate suppression, clinically signed off.** Rauland emits two INVITEs for roughly a third of events; the gateway now collapses those true duplicates within a **2-second** window, keyed to the specific bed and call purpose. The window is deliberately narrow and bed-aware: two patients coding in one room are never merged, an RRT and a Code Blue are never merged, and a genuine re-page more than two seconds later is always delivered. Every race resolves toward **delivery, never a wrongful drop**, and even a suppressed page is still recorded as an audit row.
- **Honest liveness.** A watchdog restarts the call-path service if its event loop ever hangs, and `/health` reports the real freshness of the writer's heartbeat, Fusion reachability, and how long it has been since the last inbound SIP message from Rauland — so "quiet" and "broken" are never confused.

Under it all sit non-negotiable safety invariants: test and dry-run traffic can **never** fire a real page or touch the production database, SIP is accepted only from the allowlisted clinical networks, and every credential is masked in the logs.

## Who it is for

This manual is a single shared deliverable for two audiences at once:

- **Tift Regional Medical Center** — the IT, telecom, and clinical staff who own, operate, and depend on the system.
- **RedEye Network Solutions** support — the engineers who install, monitor, and maintain it.

Both receive the same document, in full, with nothing withheld. It documents the **current production build** (`c23f3eb`, the v1.7 line). Features still in development — the high-availability epic, zero-downtime restarts, and OS-patch coordination — appear only in the clearly labeled **HA Plan** and **Roadmap** sections.

## Authoring credit

The RedEye sip2api Gateway and this manual were produced by **RedEye Network Solutions LLC, in conjunction with Claude Code.**
