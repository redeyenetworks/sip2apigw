#!/usr/bin/env python3
"""Assemble the RedEye sip2api Gateway manual: modular sources -> compiled .md + branded .docx.

Run from anywhere:  python build/build_manual.py
Requires: pandoc, node/npx (for mermaid-cli). Optional: rsvg-convert (logo in docx).
"""
import re, subprocess, datetime, pathlib, sys, os

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAN  = ROOT / "docs" / "manual"
DIST = ROOT / "docs" / "dist"
RENDER = MAN / "assets" / "rendered"
DIST.mkdir(parents=True, exist_ok=True)
RENDER.mkdir(parents=True, exist_ok=True)

PRODUCT_VER = "1.6.5-c23f3eb"
DOC_VER = "2.0"
DATE = datetime.date.today().strftime("%B %-d, %Y") if os.name != "nt" else datetime.date.today().strftime("%B %d, %Y")

ORDER = [
    "00-title.md", "01-doc-control.md", "02-glossary.md",
    "10-exec-summary.md", "11-overview.md",
    "20-architecture.md", "22-components.md",
    "30-configuration.md", "32-security.md",
    "40-operations.md", "41-monitoring-backup.md",
    "50-as-built.md",
    "60-reliability.md", "61-ha-plan.md", "62-roadmap.md",
    "70-troubleshooting.md", "71-support.md",
]

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

def demote(md):  # add one level to every ATX heading so appendix content nests under its H1
    return re.sub(r'(?m)^(#{1,6})(\s)', r'#\1\2', md)

# ---- assemble body ----
parts = []
for f in ORDER:
    p = MAN / f
    parts.append(p.read_text(encoding="utf-8") if p.exists() else f"# (missing {f})\n")

# ---- appendices from repo ----
def appendix(title, path, as_code=None):
    if not path.exists():
        return f"# {title}\n\n_(source not found: {path.name})_\n"
    body = path.read_text(encoding="utf-8")
    if as_code:
        return f"# {title}\n\n```{as_code}\n{body}\n```\n"
    return f"# {title}\n\n{demote(body)}\n"

parts.append(appendix("Appendix A — README", ROOT / "README.md"))
parts.append(appendix("Appendix B — CHANGELOG", ROOT / "CHANGELOG.md"))
cfg = ROOT / "config.yaml.example"
lk  = ROOT / "lookups.yaml"
appc = "# Appendix C — Sample Configuration Files\n\n## `config.yaml` (example)\n\n```yaml\n"
appc += (cfg.read_text(encoding="utf-8") if cfg.exists() else "# not found") + "\n```\n\n"
appc += "## `lookups.yaml` (excerpt)\n\n```yaml\n"
appc += (lk.read_text(encoding="utf-8") if lk.exists() else "# not found") + "\n```\n"
parts.append(appc)

compiled = "\n\n\\newpage\n\n".join(parts)

# ---- global polish: date + standardize SIP 481 phrasing ----
compiled = compiled.replace("{{DATE}}", DATE)
compiled = compiled.replace("Call/Transaction Does Not Exist", "Call Leg/Transaction Does Not Exist")

# ---- .md deliverable (mermaid inline, svg logo) ----
md_out = DIST / f"sip2api-gateway-manual-v{PRODUCT_VER}.md"
md_out.write_text(compiled, encoding="utf-8")
print(f"[md]   {md_out}  ({len(compiled.split())} words)")

# ---- prepare docx source: render mermaid -> png, logo svg -> png ----
counter = {"n": 0}
def render_mermaid(m):
    counter["n"] += 1
    i = counter["n"]
    src = m.group(1)
    mmd = RENDER / f"diagram_{i}.mmd"
    png = RENDER / f"diagram_{i}.png"
    mmd.write_text(src, encoding="utf-8")
    r = sh(f'npx -y @mermaid-js/mermaid-cli@11 -i "{mmd}" -o "{png}" -b white -s 2')
    if png.exists():
        print(f"[mmd]  diagram_{i}.png rendered")
        return f'\n\n![]({png.as_posix()})\n\n'
    print(f"[mmd]  diagram_{i} FAILED, keeping code block: {r.stderr[-200:]}")
    return f"```\n{src}```"

docx_src = re.sub(r"```mermaid\n(.*?)```", render_mermaid, compiled, flags=re.DOTALL)

# logo: try svg -> png for docx embedding
logo_svg = MAN / "assets" / "logo.svg"
logo_png = MAN / "assets" / "logo.png"
if logo_svg.exists():
    r = sh(f'rsvg-convert -w 640 "{logo_svg}" -o "{logo_png}"')
    if not logo_png.exists():
        r = sh(f'npx -y @mermaid-js/mermaid-cli@11 2>NUL')  # noop; placeholder
if logo_png.exists():
    docx_src = docx_src.replace("assets/logo.svg", logo_png.as_posix())
    print("[logo] embedded logo.png in docx")
else:
    # strip the SVG image ref so pandoc doesn't choke; keep the text title
    docx_src = re.sub(r'!\[[^\]]*\]\(assets/logo\.svg\)', '', docx_src)
    print("[logo] no PNG (rsvg-convert absent) -> docx uses text title only")

docx_src_file = DIST / "_docx_source.md"
docx_src_file.write_text(docx_src, encoding="utf-8")

# ---- build docx via pandoc ----
docx_out = DIST / f"sip2api-gateway-manual-v{PRODUCT_VER}.docx"
title = "RedEye sip2api Gateway — Product Manual"
author = "RedEye Network Solutions LLC, in conjunction with Claude Code"
cmd = (f'pandoc "{docx_src_file}" -o "{docx_out}" --toc --toc-depth=3 '
       f'--metadata title="{title}" --metadata author="{author}" '
       f'--metadata subtitle="Code Blue / RRT Notification Gateway - v{PRODUCT_VER}" '
       f'--resource-path="{MAN.as_posix()}"')
r = sh(cmd)
if docx_out.exists():
    print(f"[docx] {docx_out}  ({docx_out.stat().st_size//1024} KB)  diagrams={counter['n']}")
else:
    print(f"[docx] FAILED: {r.stderr[-400:]}")
    sys.exit(1)
docx_src_file.unlink(missing_ok=True)
print("DONE")
