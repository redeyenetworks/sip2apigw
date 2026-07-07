---
name: product-docs
description: Generate or update the RedEye sip2api Gateway product manual (the single shared customer + support deliverable) and compile it to .md and .docx. Use at every non-negligible release, or when asked to "regenerate/update the product documentation / manual / customer docs."
---

# product-docs — regenerate the RedEye sip2api Gateway manual

Produces ONE authoritative manual delivered identically to the customer (Tift Regional) and RedEye internal support — no secrets withheld between parties — as a versioned **`.md` + `.docx`** pair published as a **GitHub release asset**. Modular sources compile into a single document. Run this **per release**.

## Outputs
- Modular sources: `docs/manual/*.md` (each section independently maintainable) + `docs/manual/assets/logo.svg`.
- Compiled deliverables: `docs/dist/sip2api-gateway-manual-v<PRODUCT_VER>.{md,docx}`.

## Prerequisites (this box)
`pandoc`, Node/`npx` (renders Mermaid via `@mermaid-js/mermaid-cli`, and the logo via `svgexport`), Python 3 with `python-docx`. Verify with: `pandoc -v`, `npx -y @mermaid-js/mermaid-cli -V`.

## Procedure

### 1. Set the version & scope
Document **current production state**. `PRODUCT_VER` = the deployed/released version (e.g. `1.5.1`). Describe roadmap/HA/reliability-in-progress features ONLY in their labeled sections (`60-reliability`, `61-ha-plan`, `62-roadmap`) as *forthcoming* — never as current behavior. The repo source may be ahead of production; document the deployed version, not the tip.

### 2. Refresh the As-Built (run ON the production host)
The As-Built section (`docs/manual/50-as-built.md`) must reflect the live host. Gather (read-only, then mask credentials): `hostnamectl`, `uname -a`, `lscpu`, `free -h`, `df -h /var/lib /var/log`, `ip -br a`, `ip route`, `systemctl status sipgw sipgw-dashboard`, `systemctl show sipgw -p Restart,WatchdogSec,Type`, `/opt/sipgw/venv/bin/pip freeze`, `git -C /opt/sipgw rev-parse --short HEAD`, `ss -tulpn`, firewall ruleset, log dir + retention, `timedatectl`. **Never** print `client_secret`/tokens/`client_id` value.

### 3. (Re)author the section sources — multi-agent, high budget
Run one high-budget subagent PER section (they each own their area for depth/quality), each **reading the current repo** (`docs/SIPGW_SERVICE_MANUAL.md` is the primary seed — borrow & upgrade; plus README, CHANGELOG, ASSUMPTIONS, CONFIGURATION, ARCHITECTURE, `config.yaml.example`, `lookups.yaml`, `sipgw/*.py`) and writing to its `docs/manual/NN-*.md`. **Batch the agents ~3 at a time** (sequential batches) — firing all ~16 at once trips server-side rate-limiting; batching avoids it. Section list & briefs: see `build/manual_sections.md` (or the manifest in `build_manual.py`'s `ORDER`). Shared authoring rules for every agent: mask all secrets (use `<CLIENT_ID>`/`<CLIENT_SECRET>`; customer-owned IPs/scenario-id/lookups may appear); accuracy over completeness; roadmap features only in labeled sections; begin each file with `# <Title>`; embed **Mermaid** where a diagram helps (keep labels simple — avoid `<br/>`, emoji, and `[]` in sequence-diagram messages, they break the renderer). Then run one **review** agent: secret-scan + accuracy (no roadmap-as-current) + completeness.

### 4. Compile
```
python build/build_manual.py
```
This assembles the manifest order + front matter + appendices (README, CHANGELOG, sample `config.yaml`/`lookups.yaml`), substitutes `{{DATE}}`, standardizes the SIP `481` phrasing, writes the `.md` (Mermaid inline for GitHub), renders every Mermaid block to PNG and the logo SVG→PNG, and builds the branded `.docx` via pandoc (auto-TOC, title/author metadata, embedded diagrams + logo).

### 5. Gate: mechanical secret-scan (must be zero)
```
grep -cE '6ZBFCWBSJNTF|OV7XUELS|eyJhbGci|2ffd6864-3b34-11f0-a167' docs/dist/*.md
```
Any hit → STOP and fix the offending section before publishing.

### 6. Version bump & publish
Increment the doc metadata (`00-title.md`, `01-doc-control.md`) to the product version, append a **revision-history** row in `01-doc-control.md`, commit `docs/manual/` + `docs/dist/`, and attach the compiled `.md` + `.docx` as **assets on the matching GitHub release** (`gh release upload v<PRODUCT_VER> docs/dist/sip2api-gateway-manual-v<PRODUCT_VER>.{md,docx}`).

## Notes
- The manual is a **shared** deliverable — write for a mixed clinical + IT/network audience; no internal-only content.
- Diagrams are Mermaid (diff-able, regenerated each build). The `.md` renders them inline on GitHub; the `.docx` embeds rendered PNGs.
- Keep the "duplicate page OK; missed page never" reliability principle front-and-center; where a current limitation is documented, cite the roadmap item that addresses it.
