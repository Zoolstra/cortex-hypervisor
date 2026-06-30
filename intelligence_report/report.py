"""
HTML report builder. Plain string templating — no Jinja runtime, no pandas, no
Plotly. Stays inside the hypervisor container without dependency bloat.

The output mirrors the cortexhq.io design tokens: cream background, navy
text, Lora serif headings, gold accent. Two sections render real data today:

    01 — Executive summary  (appointments, revenue, sales opportunities)
    02 — Referral sources   (top revenue drivers by referral)
    03 — Patient & product  (status mix, line-item revenue split)
    04 — Inbound calls      (only when linked Invoca campaigns exist)

The Google Ads ROI cascade (§06) is now wired to ``queries.google_ads_roi`` —
per-campaign spend → clicks → calls → bookings → revenue/ROAS. The transcript
analysis drill-down sections from the Virsono report remain on the separate
"Leads" surface (``api/worklists.py``) rather than this pushed analytics report.
"""
from __future__ import annotations

import datetime as _dt
import logging
from html import escape

import plotly.graph_objects as go

from intelligence_report import queries as q

log = logging.getLogger(__name__)


# Brand palette (mirrors :root vars in _HEAD so Plotly figures match the rest
# of the report without users seeing the seams).
_CX_NAVY   = "#0a1628"
_CX_NAVY_3 = "#162a50"
_CX_CREAM  = "#f5f0e8"
_CX_GOLD   = "#d4920a"
_CX_GOLD_2 = "#e8a714"
_CX_GREEN  = "#1a8754"
_CX_LINE   = "#c9d4de"
_CX_FAINT  = "#aab6c5"
_CX_MOSS   = "#6b8e23"


_CHANNEL_PALETTE = {
    "Paid Search": _CX_GOLD,
    "Direct":      _CX_NAVY_3,
    "Organic":     _CX_MOSS,
    "Untagged":    _CX_LINE,
}


def _canonical_channel(raw: str) -> str:
    """Collapse marketing_channel / utm_medium variants into one canonical label.

    Invoca's `marketing_channel` is the source today, but historical rows and
    utm_medium-style values use mixed casing and aliases (paid, cpc, ppc, …).
    All paid-search aliases collapse to "Paid Search" so the Sankey's left
    column doesn't render the same traffic as three separate slices.
    """
    s = str(raw).strip().lower()
    if s in ("", "nan", "none", "no utm parameter data", "untagged"):
        return "Untagged"
    if s in ("paid", "cpc", "ppc", "paid search", "paid_search", "paid-search"):
        return "Paid Search"
    if s in ("organic", "organic search", "organic_search", "seo"):
        return "Organic"
    if s == "direct":
        return "Direct"
    if s == "referral":
        return "Referral"
    return raw if isinstance(raw, str) and raw.strip() else "Other"


def _bucket_channel_mix(rows: list[dict]) -> list[tuple[str, int, str]]:
    """Group the raw channel counts into the Virsono-style ordered buckets
    (Paid Search · Direct · Organic · Other · Untagged) so the Sankey's
    left column has the same shape every time, regardless of which channels
    Invoca returned.

    Raw labels are run through ``_canonical_channel`` first so paid/cpc/ppc all
    aggregate into the "Paid Search" bucket.
    """
    by_label: dict[str, int] = {}
    for r in rows:
        label = _canonical_channel(r["channel"])
        by_label[label] = by_label.get(label, 0) + int(r["count"])
    out: list[tuple[str, int, str]] = []
    for known in ("Paid Search", "Direct", "Organic"):
        if known in by_label and by_label[known] > 0:
            out.append((known, by_label.pop(known), _CHANNEL_PALETTE[known]))
    untagged = by_label.pop("Untagged", 0)
    other_total = sum(by_label.values())
    if other_total > 0:
        out.append(("Other", other_total, _CX_FAINT))
    if untagged > 0:
        out.append(("Untagged", untagged, _CX_LINE))
    return out


def _acquisition_sankey(
    medium_to_outcome: list[dict],
    patient_type: list[dict],
) -> str:
    """Plotly Sankey for the patient-acquisition funnel.

    Three columns of nodes:
      0. UTM medium       (cpc, direct, organic, untagged, …)
      1. Call outcome     (Appointment Booked / No Conversation / QLNC / Out of scope / Other)
      2. Patient type     (Existing / New / Not Found) — only Appointment Booked feeds in

    `medium_to_outcome` rows are ``{"medium", "outcome", "calls"}``;
    `patient_type` rows are ``{"patient_type", "calls"}``.

    Returns an HTML fragment (Plotly CDN script + the figure). Empty string
    when there's no data to render.
    """
    if not medium_to_outcome:
        return '<div class="empty">No callscoring data in the window — funnel hidden.</div>'

    # Build node order: mediums (by total calls), outcomes (fixed canonical
    # order), patient types (fixed order). Position via node.x so the user
    # reads left → right.
    medium_totals: dict[str, int] = {}
    for r in medium_to_outcome:
        medium_totals[r["medium"]] = medium_totals.get(r["medium"], 0) + r["calls"]
    media = [m for m, _ in sorted(medium_totals.items(), key=lambda kv: -kv[1])]

    # Outcomes present in the data, ordered canonically.
    outcomes_in_data = {r["outcome"] for r in medium_to_outcome}
    outcomes = [o for o in q.OUTCOME_LABELS if o in outcomes_in_data]

    pt_in_data = {r["patient_type"] for r in patient_type}
    pt_order = ("Existing", "New", "Not Found")
    pts = [p for p in pt_order if p in pt_in_data]

    labels = list(media) + list(outcomes) + list(pts)
    x_pos  = ([0.0] * len(media)) + ([0.5] * len(outcomes)) + ([1.0] * len(pts))

    medium_idx  = {m: i for i, m in enumerate(media)}
    outcome_idx = {o: i + len(media) for i, o in enumerate(outcomes)}
    pt_idx      = {p: i + len(media) + len(outcomes) for i, p in enumerate(pts)}

    # Node colors per column.
    outcome_color = {
        "Appointment Booked":             _CX_GREEN,
        "No Conversation":                _CX_FAINT,
        "Qualified Lead - No Conversion": _CX_GOLD_2,
        "Out of scope":                   _CX_LINE,
        "Other":                          _CX_LINE,
    }
    pt_color = {
        "Existing": _CX_NAVY_3,
        "New":      _CX_GOLD,
        "Not Found": _CX_FAINT,
    }
    node_colors = (
        [_CX_NAVY_3] * len(media)
        + [outcome_color.get(o, _CX_LINE) for o in outcomes]
        + [pt_color.get(p, _CX_LINE) for p in pts]
    )

    # Link sets.
    sources: list[int] = []
    targets: list[int] = []
    values:  list[int] = []
    link_colors: list[str] = []

    GOOD    = "rgba(212,146,10,0.40)"   # gold — flowing toward outcome
    BOOKED  = "rgba(26,135,84,0.55)"    # green — booking edge
    SOFT    = "rgba(170,182,197,0.40)"  # faint — terminal/leak
    GOLDISH = "rgba(232,167,20,0.55)"   # qualified-lead leak (warmer)

    def link_color_for(outcome: str) -> str:
        if outcome == "Appointment Booked":             return BOOKED
        if outcome == "Qualified Lead - No Conversion": return GOLDISH
        if outcome == "No Conversation":                return SOFT
        return SOFT

    for r in medium_to_outcome:
        v = int(r["calls"])
        if v <= 0:
            continue
        sources.append(medium_idx[r["medium"]])
        targets.append(outcome_idx[r["outcome"]])
        values.append(v)
        link_colors.append(link_color_for(r["outcome"]))

    pt_link_color = {
        "Existing":  "rgba(22,42,80,0.45)",
        "New":       "rgba(212,146,10,0.55)",
        "Not Found": "rgba(170,182,197,0.45)",
    }
    if "Appointment Booked" in outcome_idx:
        booked_idx = outcome_idx["Appointment Booked"]
        for r in patient_type:
            v = int(r["calls"])
            if v <= 0 or r["patient_type"] not in pt_idx:
                continue
            sources.append(booked_idx)
            targets.append(pt_idx[r["patient_type"]])
            values.append(v)
            link_colors.append(pt_link_color.get(r["patient_type"], SOFT))

    fig = go.Figure(go.Sankey(
        arrangement="snap",
        node=dict(
            pad=18, thickness=22,
            line=dict(color=_CX_NAVY, width=0.5),
            label=labels, color=node_colors, x=x_pos,
        ),
        link=dict(source=sources, target=targets, value=values, color=link_colors),
    ))
    fig.update_layout(
        paper_bgcolor=_CX_CREAM,
        plot_bgcolor=_CX_CREAM,
        font=dict(family="Geist, system-ui, sans-serif", color=_CX_NAVY, size=12),
        height=520,
        margin=dict(t=14, r=24, b=14, l=24),
    )
    return fig.to_html(
        include_plotlyjs="cdn",
        full_html=False,
        config={"displayModeBar": False},
    )


def _funnel_sankey(funnel: dict, channel_mix: list[tuple[str, int, str]]) -> str:
    """Plotly Sankey matching the Virsono ``funnel_sankey`` methodology:

        Channel Mix ▶ Inbound Calls ▶ Connected ▶ Appt Discussed ▶ Booked ▶ Invoiced

    Spam is filtered upstream (see §02), so "Inbound Calls" here is the non-
    spam call pool. The first stage decomposes into two mutually-exclusive
    branches that sum to inbound: Connected (real conversation) and No
    Conversation (voicemail / hangup / no transcript captured). Subsequent
    stages each have a faded grey "lost" branch carrying the leakage: Out of
    Scope (existing_customer or wrong_number) at the Discussed stage, and
    Qualified Lead — No Conv at the Booked stage. ``Untagged`` is shown as a
    real channel so the chart surfaces how much attribution we'd unlock with
    better tracking-pixel coverage.
    """
    calls     = int(funnel.get("calls", 0))
    voicemail = int(funnel.get("voicemail_hangup", 0))
    answered  = int(funnel.get("answered", 0))
    discussed = int(funnel.get("discussed", 0))
    booked    = int(funnel.get("booked", 0))
    invoiced  = int(funnel.get("invoiced", 0))

    if calls == 0:
        return '<div class="stub">No calls in the window — funnel chart hidden.</div>'

    if not channel_mix:
        channel_mix = [("Inbound", calls, _CX_NAVY_3)]

    n = len(channel_mix)
    inbound_idx     = n
    connected_idx   = n + 1
    voicemail_idx   = n + 2
    discussed_idx   = n + 3
    no_appt_idx     = n + 4
    booked_idx      = n + 5
    not_booked_idx  = n + 6
    invoiced_idx    = n + 7
    not_inv_idx     = n + 8

    labels = [c[0] for c in channel_mix] + [
        "Inbound Calls",
        "Connected", "No Conversation",
        "Appt Discussed", "Out of Scope",
        "Appt Booked", "Qualified Lead — No Conv",
        "Invoiced", "Booked, Not Yet Invoiced",
    ]
    colors = [c[2] for c in channel_mix] + [
        _CX_NAVY_3,
        _CX_GOLD,                                      # Connected
        _CX_LINE,                                      # Voicemail
        _CX_GOLD,   _CX_LINE,                          # Discussed, No Appt Topic
        _CX_GOLD_2, _CX_LINE,                          # Booked, Not Booked
        _CX_GREEN,  _CX_LINE,                          # Invoiced, Not Yet Invoiced
    ]

    sources: list[int] = []
    targets: list[int] = []
    values: list[int] = []
    link_colors: list[str] = []

    GOOD  = "rgba(212,146,10,0.40)"   # gold
    GOOD2 = "rgba(232,167,20,0.55)"   # brighter mid-funnel
    LOST  = "rgba(192,57,43,0.18)"    # faded red leak
    WIN   = "rgba(26,135,84,0.55)"    # green to revenue

    channel_link_color = {
        _CX_GOLD:   "rgba(212,146,10,0.40)",
        _CX_NAVY_3: "rgba(22,42,80,0.30)",
        _CX_MOSS:   "rgba(107,142,35,0.40)",
        _CX_FAINT:  "rgba(170,182,197,0.35)",
        _CX_LINE:   "rgba(201,212,222,0.45)",
    }

    def add(s: int, t: int, v: int, color: str) -> None:
        if v > 0:
            sources.append(s); targets.append(t); values.append(v); link_colors.append(color)

    for i, (lbl, count, color) in enumerate(channel_mix):
        add(i, inbound_idx, count, channel_link_color.get(color, "rgba(100,100,100,0.3)"))

    add(inbound_idx,  connected_idx, answered,           GOOD)
    add(inbound_idx,  voicemail_idx, voicemail,          LOST)
    add(connected_idx, discussed_idx, discussed,         GOOD)
    add(connected_idx, no_appt_idx,  answered - discussed, LOST)
    add(discussed_idx, booked_idx,   booked,             GOOD2)
    add(discussed_idx, not_booked_idx, discussed - booked, LOST)
    add(booked_idx,   invoiced_idx,  invoiced,           WIN)
    add(booked_idx,   not_inv_idx,   booked - invoiced,  LOST)

    fig = go.Figure(go.Sankey(
        arrangement="snap",
        node=dict(
            pad=18, thickness=22,
            line=dict(color=_CX_NAVY, width=0.5),
            label=labels, color=colors,
        ),
        link=dict(source=sources, target=targets, value=values, color=link_colors),
    ))
    fig.update_layout(
        paper_bgcolor=_CX_CREAM,
        plot_bgcolor=_CX_CREAM,
        font=dict(family="Geist, system-ui, sans-serif", color=_CX_NAVY, size=12),
        height=540,
        margin=dict(t=14, r=24, b=14, l=24),
    )
    return fig.to_html(
        include_plotlyjs="cdn",
        full_html=False,
        config={"displayModeBar": False},
    )


def _webform_funnel_sankey(funnel: dict) -> str:
    """Plotly Sankey for the web-form funnel: UTM source ▶ Submissions ▶ Invoiced.

    Left column = one node per UTM source (value = submissions), funnelling into
    a Submissions pool, then splitting into Invoiced (green, converted to a
    paying patient) and No Invoice Match (faded grey drop-off). Mirrors
    :func:`_funnel_sankey`'s construction and styling.
    """
    sources_data = funnel.get("sources", [])
    total_sub = int(funnel.get("total_submissions", 0))
    total_inv = int(funnel.get("total_invoiced", 0))
    if total_sub == 0:
        return '<div class="stub">No web-form submissions in the window — funnel hidden.</div>'

    n = len(sources_data)
    submissions_idx = n
    invoiced_idx    = n + 1
    no_inv_idx      = n + 2

    labels = [s["source"] for s in sources_data] + [
        "Submissions", "Invoiced", "No Invoice Match",
    ]
    # Source nodes navy, Submissions gold, Invoiced green, drop-off grey line.
    colors = [_CX_NAVY_3] * n + [_CX_GOLD, _CX_GREEN, _CX_LINE]

    GOLD = "rgba(212,146,10,0.40)"
    WIN  = "rgba(26,135,84,0.55)"
    LOST = "rgba(201,212,222,0.45)"

    sources: list[int] = []
    targets: list[int] = []
    values: list[int] = []
    link_colors: list[str] = []

    def add(s: int, t: int, v: int, color: str) -> None:
        if v > 0:
            sources.append(s); targets.append(t); values.append(v); link_colors.append(color)

    for i, s in enumerate(sources_data):
        add(i, submissions_idx, int(s["submissions"]), "rgba(22,42,80,0.30)")
    add(submissions_idx, invoiced_idx, total_inv, WIN)
    add(submissions_idx, no_inv_idx, total_sub - total_inv, LOST)

    fig = go.Figure(go.Sankey(
        arrangement="snap",
        node=dict(
            pad=18, thickness=22,
            line=dict(color=_CX_NAVY, width=0.5),
            label=labels, color=colors,
        ),
        link=dict(source=sources, target=targets, value=values, color=link_colors),
    ))
    fig.update_layout(
        paper_bgcolor=_CX_CREAM,
        plot_bgcolor=_CX_CREAM,
        font=dict(family="Geist, system-ui, sans-serif", color=_CX_NAVY, size=12),
        height=420,
        margin=dict(t=14, r=24, b=14, l=24),
    )
    return fig.to_html(
        include_plotlyjs="cdn",
        full_html=False,
        config={"displayModeBar": False},
    )


# ── CSS scaffolding ──────────────────────────────────────────────────────────

_HEAD = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@500&family=Lora:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --cx-navy:    #0a1628;
    --cx-navy-2:  #0d1f38;
    --cx-navy-3:  #162a50;
    --cx-cream:   #f5f0e8;
    --cx-cream-2: #ede7d8;
    --cx-cream-3: #e3dcc8;
    --cx-rule:    #d0dde8;
    --cx-line:    #c9d4de;
    --cx-mute:    #6b7a8f;
    --cx-faint:   #aab6c5;
    --cx-text-2:  rgba(10,22,40,0.68);
    --cx-gold:    #d4920a;
    --cx-gold-2:  #e8a714;
    --cx-green:   #1a8754;
    --cx-red:     #c0392b;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0;
    font-family: "Geist", system-ui, -apple-system, sans-serif;
    color: var(--cx-navy);
    background: var(--cx-cream);
    line-height: 1.55;
    font-size: 15px;
    letter-spacing: -0.005em;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: 28px 24px 48px; }}
  header.report-header {{
    background: var(--cx-navy);
    color: var(--cx-cream);
    padding: 32px 24px;
    margin: -28px -24px 28px;
    border-radius: 0;
  }}
  header .eyebrow {{
    font-family: "Geist Mono", monospace; font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.16em;
    color: var(--cx-gold); font-weight: 500;
  }}
  header h1 {{
    font-family: "Lora", Georgia, serif; font-weight: 500;
    font-size: 38px; letter-spacing: -0.01em;
    margin: 8px 0 4px;
    color: var(--cx-cream);
  }}
  header .meta {{
    color: rgba(245,240,232,0.72);
    font-family: "Geist Mono", monospace; font-size: 12px;
  }}
  section {{
    background: white;
    border: 1px solid var(--cx-rule);
    border-radius: 6px;
    padding: 28px;
    margin-bottom: 20px;
    box-shadow: 0 1px 2px rgba(10,22,40,0.06);
  }}
  section h2 {{
    font-family: "Lora", Georgia, serif; font-weight: 500;
    font-size: 22px; letter-spacing: -0.005em;
    margin: 0 0 6px;
    display: flex; align-items: baseline; gap: 12px;
  }}
  section h2 .num {{
    font-family: "Geist Mono", monospace; font-size: 11px; font-weight: 500;
    color: var(--cx-gold);
    background: rgba(212,146,10,0.08);
    padding: 3px 8px; border-radius: 3px;
    letter-spacing: 0.06em;
  }}
  section .lede {{ color: var(--cx-text-2); margin: 0 0 18px; font-size: 14px; }}

  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 0 0 18px; }}
  .stat {{
    background: var(--cx-cream);
    border-left: 3px solid var(--cx-gold);
    padding: 12px 14px; border-radius: 4px;
  }}
  .stat .label {{
    font-family: "Geist Mono", monospace; font-size: 10px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--cx-mute);
  }}
  .stat .value {{ font-family: "Lora", Georgia, serif; font-size: 24px; font-weight: 500; margin-top: 4px; color: var(--cx-navy); }}
  .stat .sub {{ font-size: 12px; color: var(--cx-mute); margin-top: 2px; }}

  table.cx {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  table.cx th {{
    text-align: left; padding: 8px 10px;
    background: var(--cx-navy); color: var(--cx-cream);
    font-family: "Geist Mono", monospace; font-weight: 500; font-size: 11px;
    letter-spacing: 0.04em; text-transform: uppercase;
  }}
  table.cx td {{
    padding: 7px 10px; border-top: 1px solid var(--cx-rule);
    color: var(--cx-navy);
  }}
  table.cx tr:nth-child(even) td {{ background: var(--cx-cream-2); }}
  table.cx td.num {{ text-align: right; font-variant-numeric: tabular-nums; font-family: "Geist Mono", monospace; font-size: 12px; }}
  table.cx td.muted {{ color: var(--cx-mute); }}

  .bar-row {{ display: grid; grid-template-columns: 200px 1fr 110px; gap: 8px; align-items: center; padding: 4px 0; font-size: 13px; }}
  .bar-row .label {{ color: var(--cx-navy); }}
  .bar-row .bar-track {{ background: var(--cx-cream-2); height: 8px; border-radius: 999px; overflow: hidden; }}
  .bar-row .bar-fill {{ background: var(--cx-gold); height: 100%; }}
  .bar-row .count {{ text-align: right; font-family: "Geist Mono", monospace; font-size: 12px; color: var(--cx-mute); }}

  .empty {{ color: var(--cx-mute); padding: 18px; text-align: center; background: var(--cx-cream); border-radius: 4px; }}
  .stub {{
    background: var(--cx-cream);
    border-left: 3px solid var(--cx-faint);
    padding: 14px 16px; border-radius: 4px;
    color: var(--cx-text-2); font-size: 13px;
  }}
  .stub b {{ color: var(--cx-navy); font-weight: 600; }}

  /* ROI cascade */
  .roi-row {{
    background: var(--cx-cream);
    border-radius: 6px;
    padding: 16px 18px;
    border: 1px solid var(--cx-rule);
    margin-bottom: 12px;
  }}
  .roi-header {{
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 12px; gap: 8px; flex-wrap: wrap;
  }}
  .roi-header .name {{
    font-family: "Lora", serif; font-weight: 500; color: var(--cx-navy); font-size: 18px;
  }}
  .roi-header .summary {{
    color: var(--cx-mute); font-size: 11px;
    font-family: "Geist Mono", monospace; letter-spacing: 0.02em;
  }}
  .roi-header .summary b {{ color: var(--cx-navy); font-weight: 500; }}
  .roi-stages {{ display: flex; align-items: stretch; gap: 0; }}
  .roi-stage {{
    flex: 1.1;
    padding: 12px 8px;
    border-radius: 4px;
    background: white;
    border: 1px solid var(--cx-rule);
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    text-align: center; min-height: 84px;
    border-top: 3px solid var(--cx-line);
  }}
  .roi-stage .label {{
    font-family: "Geist Mono", monospace; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--cx-mute); font-weight: 500;
  }}
  .roi-stage .value {{
    font-family: "Lora", serif; font-size: 22px; font-weight: 500;
    margin-top: 4px; color: var(--cx-navy); line-height: 1.05;
  }}
  .roi-stage .sub {{
    font-size: 10px; color: var(--cx-mute); margin-top: 3px;
    font-family: "Geist Mono", monospace;
  }}
  .roi-stage[data-stage="clicks"]   {{ border-top-color: var(--cx-navy-3); }}
  .roi-stage[data-stage="calls"]    {{ border-top-color: var(--cx-gold); }}
  .roi-stage[data-stage="bookings"] {{ border-top-color: var(--cx-gold-2); }}
  .roi-arrow {{
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    padding: 0 5px; flex: 0.55;
  }}
  .roi-arrow .arrow {{ font-size: 20px; color: var(--cx-line); line-height: 1; }}
  .roi-arrow .conv {{
    font-family: "Geist Mono", monospace;
    font-size: 11px; color: var(--cx-navy); margin-top: 5px; font-weight: 600; text-align: center;
  }}
  .roi-arrow .cost {{
    font-family: "Geist Mono", monospace;
    font-size: 10px; color: var(--cx-mute); margin-top: 2px; text-align: center;
  }}

  /* Funnel */
  .funnel {{ display: flex; flex-direction: column; gap: 8px; }}
  .funnel-row {{
    display: grid; grid-template-columns: 130px 1fr 90px 70px; gap: 10px;
    align-items: center; font-size: 13px;
  }}
  .funnel-row .stage-name {{ color: var(--cx-navy); font-weight: 500; }}
  .funnel-row .bar-track {{
    background: var(--cx-cream-2); height: 16px; border-radius: 999px; overflow: hidden;
  }}
  .funnel-row .bar-fill {{
    background: linear-gradient(90deg, var(--cx-gold) 0%, var(--cx-gold-2) 100%);
    height: 100%; transition: width 0.3s;
  }}
  .funnel-row .count {{ text-align: right; font-family: "Geist Mono", monospace; font-size: 12px; color: var(--cx-navy); }}
  .funnel-row .pct {{ text-align: right; font-family: "Geist Mono", monospace; font-size: 11px; color: var(--cx-mute); }}

  footer {{
    color: var(--cx-mute); font-size: 11px;
    margin-top: 24px; padding-top: 16px;
    border-top: 1px solid var(--cx-rule);
    font-family: "Geist Mono", monospace; letter-spacing: 0.04em;
  }}
</style></head><body>
<div class="wrap">
"""

_FOOT = "</div></body></html>"


def _fmt_money(v: float) -> str:
    return f"${v:,.0f}"


def _fmt_int(v: int) -> str:
    return f"{v:,}"


def _section(num: int, title: str, lede: str, body: str) -> str:
    return (
        f'<section><h2><span class="num">{num:02d}</span>{escape(title)}</h2>'
        f'<p class="lede">{lede}</p>{body}</section>'
    )


def _stat(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="sub">{escape(sub)}</div>' if sub else ""
    return (
        f'<div class="stat"><div class="label">{escape(label)}</div>'
        f'<div class="value">{value}</div>{sub_html}</div>'
    )


def _bar_chart(rows: list[tuple[str, int]]) -> str:
    """Inline CSS bar chart. rows = [(label, count), …] — already sorted."""
    if not rows:
        return '<div class="empty">No data in window.</div>'
    max_val = max(r[1] for r in rows) or 1
    bars = []
    for label, count in rows:
        pct = (count / max_val) * 100
        bars.append(
            f'<div class="bar-row">'
            f'<div class="label">{escape(label)}</div>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%"></div></div>'
            f'<div class="count">{count:,}</div>'
            f'</div>'
        )
    return "".join(bars)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<div class="empty">No data in window.</div>'
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f'<td class="num">{c}</td>' if i > 0 else f"<td>{c}</td>"
                          for i, c in enumerate(r)) + "</tr>"
        for r in rows
    )
    return f'<table class="cx"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


# ── Sections ─────────────────────────────────────────────────────────────────

def _section_acquisition(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    google_ads_campaign_ids: list[str],
    days: int,
) -> str:
    """Acquisition · drivers of call traffic. First stage of the
    patient-acquisition page (see patient_acquisition_data_model memo).

    Two stat tiles (ad-driven share + count) followed by two top-10 tables
    (regions, keywords) drawn from ``ad_clicks_v2`` via the gclid join.
    """
    traffic = q.acquisition_call_traffic(clinic_id, invoca_campaign_ids, days=days)
    regions = q.top_calling_regions(
        clinic_id, invoca_campaign_ids, google_ads_campaign_ids, days=days, top_n=10
    )
    keywords = q.top_keywords(
        clinic_id, invoca_campaign_ids, google_ads_campaign_ids, days=days, top_n=10
    )

    stats = (
        '<div class="stats">'
        + _stat(
            "Ad-driven share",
            f"{traffic['ad_driven_pct']:.1f}%",
            f"of {_fmt_int(traffic['total_calls'])} total calls",
        )
        + _stat(
            "Ad-driven calls",
            _fmt_int(traffic["ad_driven_calls"]),
            "transactions with a gclid",
        )
        + _stat(
            "Total calls",
            _fmt_int(traffic["total_calls"]),
            f"last {days}d",
        )
        + "</div>"
    )

    regions_table = _table(
        ["Region", "Calls"],
        [[escape(r["region"]), _fmt_int(r["calls"])] for r in regions],
    )
    keywords_table = _table(
        ["Keyword", "Calls"],
        [[escape(r["keyword"]), _fmt_int(r["calls"])] for r in keywords],
    )

    body = (
        stats
        + '<h3 style="margin-top:24px">Top 10 calling regions</h3>'
        + regions_table
        + '<h3 style="margin-top:24px">Top 10 keywords</h3>'
        + keywords_table
    )

    return _section(
        1,
        "Acquisition · drivers of call traffic",
        f"Inbound call volume over the last <b>{days}</b> days. Ad-driven calls "
        "are phone calls which are linked to a Google click ID.",
        body,
    )


def _headline_fallback(m: dict) -> str:
    """Deterministic one-liner if the LLM is unavailable: lead with the biggest
    month-over-month mover among the rates / revenue."""
    last, prior = m["last"], m["prior"]
    ll, pl = last["label"], prior["label"]
    moves = []  # (name, prior, last, is_rate)
    if last["capture_rate"] is not None and prior["capture_rate"] is not None:
        moves.append(("Phone call capture rate", prior["capture_rate"], last["capture_rate"], True))
    if last["form_rate"] is not None and prior["form_rate"] is not None:
        moves.append(("Form response rate", prior["form_rate"], last["form_rate"], True))
    if prior["revenue"] and last["revenue"]:
        moves.append(("Revenue", prior["revenue"], last["revenue"], False))
    if not moves:
        return f"Not enough month-over-month data to call a headline for {ll} yet."
    name, p, l, is_rate = max(moves, key=lambda x: abs((x[2] - x[1]) / x[1]) if x[1] else 0)
    direction = "up" if l >= p else "down"
    if is_rate:
        return (f"{name} moved {direction} from {p * 100:.0f}% to {l * 100:.0f}% "
                f"in {ll} versus {pl} — the number to watch this month.")
    pct = abs((l - p) / p) * 100 if p else 0
    return f"Revenue is {direction} {pct:.0f}% in {ll} versus {pl} — the headline read this month."


def _headline_sentence(clinic_name: str, m: dict) -> str:
    """One-sentence 'the thing that matters' read, written by Claude from the
    month-over-month numbers. Falls back to a deterministic line on any failure
    so the report never breaks."""
    fallback = _headline_fallback(m)
    last, prior = m["last"], m["prior"]

    def pct(v):
        return f"{v * 100:.0f}%" if v is not None else "n/a"

    facts = (
        f"Clinic: {clinic_name}. Month-over-month, comparing {last['label']} "
        f"(most recent full month) to {prior['label']}.\n"
        f"- Phone call capture rate (booked / connected calls): "
        f"{pct(prior['capture_rate'])} -> {pct(last['capture_rate'])} "
        f"({prior['connected']} -> {last['connected']} connected, "
        f"{prior['booked']} -> {last['booked']} booked).\n"
        f"- Form response rate (submissions with a later appointment / submissions): "
        f"{pct(prior['form_rate'])} -> {pct(last['form_rate'])} "
        f"({prior['submissions']} -> {last['submissions']} submissions).\n"
        f"- Total inbound calls: {prior['calls']} -> {last['calls']}.\n"
        f"- Invoiced revenue: ${prior['revenue']:,.0f} -> ${last['revenue']:,.0f}."
    )
    prompt = (
        "You are writing the single headline read for a hearing-clinic owner's "
        "monthly report. From the month-over-month figures below, write ONE "
        "sentence (max 30 words) naming the most important thing that changed: "
        "lead with the direction/trend, plain language, a specific number, no "
        "preamble, no hedging. Return only the sentence.\n\n" + facts
    )
    try:
        import anthropic
        from api.core.secrets import get_secret
        key = (get_secret("anthropic-api-key") or "").strip()
        if not key:
            return fallback
        client = anthropic.Anthropic(api_key=key, timeout=12.0)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=90,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
        return text or fallback
    except Exception as e:
        log.warning("headline LLM failed clinic=%s: %s", clinic_name, e)
        return fallback


def _section_headline(
    clinic_id: str,
    clinic_name: str,
    invoca_campaign_ids: list[str],
) -> str:
    """Top-of-report headline: the one thing that matters + the MoM KPI band.

    Compares the last fully-elapsed month to the month before it. KPIs: phone
    call capture rate and form response rate (voicemail response rate is deferred
    until calls are classified for voicemail). The lead sentence is LLM-written.
    """
    m = q.headline_metrics(clinic_id, invoca_campaign_ids)
    last, prior = m["last"], m["prior"]

    has_calls = bool(last["connected"] or prior["connected"])
    has_forms = bool(last["submissions"] or prior["submissions"])
    if not has_calls and not has_forms:
        body = (
            '<div class="empty">Not enough call or web-form activity in '
            f'{prior["label"]}–{last["label"]} to compute a month-over-month headline.</div>'
        )
        return _section(0, "The headline", f"{last['label']} vs {prior['label']}", body)

    def kpi(label: str, p, l, denom_note: str) -> str:
        if l is None and p is None:
            return _stat(label, "—", denom_note)
        value = f"{l * 100:.0f}%" if l is not None else "—"
        if p is not None and l is not None:
            delta = (l - p) * 100
            arrow = "▲" if delta >= 0 else "▼"
            sub = f"{p * 100:.0f}% → {l * 100:.0f}%   {arrow} {abs(delta):.0f} pts"
        elif p is not None:
            sub = f"was {p * 100:.0f}% · no data {last['label']}"
        else:
            sub = denom_note
        return _stat(label, value, sub)

    sentence = _headline_sentence(clinic_name, m)

    cards = '<div class="stats" style="margin-top:16px;">'
    if has_calls:
        cards += kpi("Phone call capture rate", prior["capture_rate"], last["capture_rate"],
                     "booked ÷ connected calls")
    if has_forms:
        cards += kpi("Form response rate", prior["form_rate"], last["form_rate"],
                     "submissions with a later appt ÷ submissions")
    cards += "</div>"

    voicemail_note = (
        '<p class="lede" style="margin-top:12px;color:var(--cx-mute);font-size:12px;">'
        "Voicemail response rate is coming once calls are classified for voicemail.</p>"
    )
    headline_read = (
        f'<p style="font-family:Georgia,\'Times New Roman\',serif;font-size:22px;'
        f'line-height:1.4;color:var(--cx-navy);margin:6px 0 0;">{escape(sentence)}</p>'
    )
    body = headline_read + cards + voicemail_note
    return _section(
        0, "The headline",
        f"The one read that matters · {last['label']} vs {prior['label']} (month over month)",
        body,
    )


def _utm_filter_control(
    sources: list[dict],
    mediums: list[dict],
    cur_sources: list[str],
    cur_mediums: list[str],
) -> str:
    """Multi-select filter for the call funnel: pick any ``utm_source`` and/or
    ``utm_medium`` values to include. "Apply" posts the selection up to the parent
    frame (the report is a srcDoc iframe with no navigable URL of its own), which
    re-fetches with ``?utm_source=&utm_medium=``.
    """
    def boxes(cls: str, options: list[dict], current: list[str]) -> str:
        cur = set(current)
        items = []
        for o in options:
            v = o["value"]
            checked = " checked" if v in cur else ""
            items.append(
                '<label style="display:inline-flex;align-items:center;gap:4px;'
                'margin:0 10px 6px 0;font-size:13px;color:var(--cx-navy);white-space:nowrap;">'
                f'<input type="checkbox" class="{cls}" value="{escape(v)}"{checked}>'
                f'{escape(v)} <span style="color:var(--cx-mute);">({_fmt_int(o["calls"])})</span>'
                '</label>'
            )
        return "".join(items) or '<span style="color:var(--cx-mute);font-size:13px;">none</span>'

    # Single-quoted JS so it sits inside double-quoted attributes unescaped.
    collect = ("(function(){var g=function(c){return [].slice.call("
               "document.querySelectorAll(c)).map(function(x){return x.value})};"
               "parent.postMessage({type:'cortex:utm-filter',"
               "sources:g('.cx-utm-src:checked'),mediums:g('.cx-utm-med:checked')},'*')})()")
    clear = ("(function(){[].slice.call(document.querySelectorAll('.cx-utm-src,.cx-utm-med'))"
             ".forEach(function(x){x.checked=false});"
             "parent.postMessage({type:'cortex:utm-filter',sources:[],mediums:[]},'*')})()")

    btn = ("font-family:inherit;font-size:12px;font-weight:600;padding:5px 12px;"
           "border-radius:6px;border:1px solid var(--cx-line);cursor:pointer;")
    return (
        '<div style="border:1px solid var(--cx-line);border-radius:8px;padding:12px 14px;'
        'margin-bottom:16px;background:#fff;">'
        '<div style="font-size:12px;font-weight:600;color:var(--cx-navy);margin-bottom:8px;">'
        'Filter calls to include &nbsp;'
        '<span style="font-weight:400;color:var(--cx-mute);">'
        '(source AND medium; leave a row empty for no constraint)</span></div>'
        '<div style="margin-bottom:4px;"><span style="display:inline-block;min-width:62px;'
        'font-size:12px;color:var(--cx-mute);">Sources</span>'
        + boxes("cx-utm-src", sources, cur_sources) + '</div>'
        '<div style="margin-bottom:10px;"><span style="display:inline-block;min-width:62px;'
        'font-size:12px;color:var(--cx-mute);">Mediums</span>'
        + boxes("cx-utm-med", mediums, cur_mediums) + '</div>'
        f'<button onclick="{collect}" style="{btn}background:var(--cx-gold,#d4920a);'
        'color:#fff;border-color:transparent;">Apply filter</button> '
        f'<button onclick="{clear}" style="{btn}background:#fff;color:var(--cx-navy);">Clear</button>'
        '</div>'
    )


def _utm_filter_label(cur_sources: list[str], cur_mediums: list[str]) -> str:
    bits = []
    if cur_sources:
        bits.append("source: " + ", ".join(cur_sources))
    if cur_mediums:
        bits.append("medium: " + ", ".join(cur_mediums))
    return " · ".join(bits) if bits else "all calls"


def _section_funnel(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    google_ads_campaign_ids: list[str],
    days: int,
    utm_sources: list[str] | None = None,
    utm_mediums: list[str] | None = None,
) -> str:
    """End-to-end patient-acquisition funnel — Virsono methodology.

    Channel Mix ▶ Inbound Calls ▶ Connected ▶ Appt Discussed ▶ Booked ▶ Invoiced.
    Drop-offs branch off at each stage (Voicemail/Hangup, No Appt Topic, Not
    Booked, Booked-Not-Invoiced) so totals reconcile visually. Invoiced is the
    call→patient→appointment→invoice chain matched against Blueprint_PHI; see
    ``queries.revenue_funnel`` for the join logic.

    No-Conversation and Qualified-Lead-No-Conv drill-down buttons are kept and
    derived from the callscoring crosstab (orthogonal to the Invoca-flag-driven
    Sankey).

    Web-form submissions (``ClinicData.webforms``) are surfaced as a lead source
    *parallel* to inbound calls in a top-of-section band (total + new/returning),
    so total leads = calls + form submissions. Form volume is shown even when the
    clinic has no linked Invoca campaigns or no calls; it is not (yet) threaded
    into the call→booking→invoice Sankey.
    """
    forms = q.webform_submissions(clinic_id, days=days)
    form_total = int(forms.get("total", 0))

    # Normalize the multi-select UTM filters (lowercase/trim/dedupe, drop blanks).
    utm_sources = q._utm_clean(utm_sources)
    utm_mediums = q._utm_clean(utm_mediums)

    # Filter options (only meaningful with linked Invoca campaigns).
    src_opts = (
        q.funnel_utm_sources(clinic_id, invoca_campaign_ids, days=days)
        if invoca_campaign_ids else []
    )
    med_opts = (
        q.funnel_utm_mediums(clinic_id, invoca_campaign_ids, days=days)
        if invoca_campaign_ids else []
    )
    filter_control = (
        _utm_filter_control(src_opts, med_opts, utm_sources, utm_mediums)
        if invoca_campaign_ids else ""
    )
    filtered = bool(utm_sources or utm_mediums)

    def _lead_sources_band(calls: int) -> str:
        """Top-of-funnel summary: web-form leads alongside inbound calls.

        Volume only — web-form revenue attribution lives in its own section
        (``_section_webform_revenue``)."""
        if form_total <= 0:
            return ""
        bits = []
        if forms.get("new"):
            bits.append(f"{_fmt_int(int(forms['new']))} new")
        if forms.get("returning"):
            bits.append(f"{_fmt_int(int(forms['returning']))} returning")
        form_sub = " · ".join(bits) if bits else f"last {days}d"
        return (
            '<div class="stats">'
            + _stat("Web form submissions", _fmt_int(form_total), form_sub)
            + _stat("Inbound calls", _fmt_int(calls), f"non-spam · last {days}d")
            + _stat("Total leads", _fmt_int(calls + form_total), "calls + web forms")
            + "</div>"
        )

    _lede = (
        "Web-form and call lead sources, then the call funnel: channel mix → "
        "call → connected → discussed → booked → invoiced."
    )

    if not invoca_campaign_ids:
        note = (
            '<div class="stub">'
            '<b>No Invoca campaigns linked.</b> Link campaigns from '
            '<em>Manage instance → Campaigns</em> and the call funnel will render.'
            '</div>'
        )
        band = _lead_sources_band(0)
        return _section(
            3, "Funnel · end-to-end patient acquisition", _lede,
            (band + note) if band else note,
        )

    funnel = q.revenue_funnel(
        clinic_id, google_ads_campaign_ids, invoca_campaign_ids, days=days,
        utm_sources=utm_sources, utm_mediums=utm_mediums,
    )
    calls = int(funnel.get("calls", 0))

    if calls == 0:
        band = _lead_sources_band(0)
        note = (
            f'<div class="empty">No calls for <b>{escape(_utm_filter_label(utm_sources, utm_mediums))}</b> '
            'in the window — adjust the filter below.</div>'
            if filtered else
            '<div class="empty">No calls in the window — call funnel hidden.</div>'
        )
        return _section(
            3, "Funnel · end-to-end patient acquisition", _lede,
            band + filter_control + note,
        )

    spam      = int(funnel.get("spam", 0))
    answered  = int(funnel.get("answered", 0))
    discussed = int(funnel.get("discussed", 0))
    booked    = int(funnel.get("booked", 0))
    invoiced  = int(funnel.get("invoiced", 0))
    revenue   = float(funnel.get("matched_revenue", 0.0))

    channels = _bucket_channel_mix(
        q.channel_mix(clinic_id, invoca_campaign_ids, days=days,
                      utm_sources=utm_sources, utm_mediums=utm_mediums)
    )

    # Drill-down button gating — callscoring-based, orthogonal to the Sankey.
    flows = q.funnel_medium_to_outcome(clinic_id, invoca_campaign_ids, days=days)
    no_conv = sum(r["calls"] for r in flows if r["outcome"] == "No Conversation")
    qlnc = sum(
        r["calls"] for r in flows
        if r["outcome"] == "Qualified Lead - No Conversion"
    )

    def _pct(num: int, denom: int) -> str:
        return f"{(100 * num / denom):.0f}% of prior" if denom else ""

    stage_stats = (
        '<div class="stats">'
        + _stat(
            "Inbound calls",
            _fmt_int(calls),
            f"non-spam · last {days}d"
            + (f" · {_fmt_int(spam)} spam filtered" if spam > 0 else ""),
        )
        + _stat("Connected", _fmt_int(answered), _pct(answered, calls))
        + _stat("Appt discussed", _fmt_int(discussed), _pct(discussed, answered))
        + _stat("Booked", _fmt_int(booked), _pct(booked, discussed))
        + _stat("Invoiced", _fmt_int(invoiced), _pct(invoiced, booked))
        + "</div>"
    )

    # Inline note above the Sankey when there is spam to disclose. The §02
    # block above this section carries the spam roll-up + detail-page link.
    spam_note = ""
    if spam > 0:
        spam_note = (
            '<p class="lede" style="margin-top:14px;">'
            f'<b>{_fmt_int(spam)}</b> spam-classified call'
            f"{'s' if spam != 1 else ''} filtered out of this funnel · "
            'see §02 above for the breakdown and per-call detail.'
            '</p>'
        )

    rev_scope = (
        f"{_utm_filter_label(utm_sources, utm_mediums)} · last {days}d" if filtered
        else f"invoices for matched patients · last {days}d"
    )
    revenue_stats = (
        '<div class="stats" style="margin-top:18px;">'
        + _stat("Matched revenue", _fmt_money(revenue), rev_scope)
        + _stat("Avg per invoiced call",
                _fmt_money(revenue / invoiced) if invoiced else "—")
        + "</div>"
    )

    sankey = _funnel_sankey(funnel, channels)

    lead_band = _lead_sources_band(calls)
    body = (
        lead_band
        + filter_control
        + stage_stats
        + spam_note
        + '<div style="margin-top:18px;">' + sankey + "</div>"
        + revenue_stats
    )
    return _section(
        3,
        "Funnel · end-to-end patient acquisition",
        "Web-form and call lead sources, then where each call ended up — "
        "connected, booked, and ultimately invoiced — or where it dropped off "
        "along the way.",
        body,
    )


def _section_webform_revenue(clinic_id: str, days: int) -> str:
    """Web-form funnel — UTM source → submissions → invoice (its own section).

    Submitters in ``ClinicData.webforms`` are matched to Blueprint patients by
    phone or email; invoices dated on/after the submission convert them. The
    funnel Sankey breaks submissions down by UTM source and shows how many flow
    through to a paying patient; a per-source table carries the numbers. This is
    correlational attribution — it credits forms whose submitter later
    transacted, not proof the form drove the visit.
    """
    title = "Web-form funnel · UTM → submissions → invoice"
    lede = (
        "Where web-form leads come from (UTM source), how many submit, and how "
        "many convert to a paying patient — matched to Blueprint by phone or "
        "email, invoices dated on or after submission. Correlational: it credits "
        "forms whose submitter later transacted, not proof the form drove the visit."
    )
    forms = q.webform_submissions(clinic_id, days=days)
    form_total = int(forms.get("total", 0))
    if form_total == 0:
        return _section(
            4, title, lede,
            '<div class="empty">No web-form submissions in the window.</div>',
        )

    rev = q.webform_revenue(clinic_id, days=days)
    matched       = int(rev.get("matched_patients", 0))
    invoiced      = int(rev.get("invoiced_patients", 0))
    invoice_count = int(rev.get("invoice_count", 0))
    revenue       = float(rev.get("attributed_revenue", 0.0))

    stats = (
        '<div class="stats">'
        + _stat("Web-form submissions", _fmt_int(form_total), f"last {days}d")
        + _stat("Matched patients", _fmt_int(matched),
                f"{(100 * matched / form_total):.0f}% of submissions" if form_total else "")
        + _stat("Invoiced patients", _fmt_int(invoiced),
                f"{(100 * invoiced / matched):.0f}% of matched" if matched else "")
        + _stat("Attributed revenue", _fmt_money(revenue),
                f"{_fmt_int(invoice_count)} invoice{'' if invoice_count == 1 else 's'}")
        + _stat("Avg per invoiced patient",
                _fmt_money(revenue / invoiced) if invoiced else "—")
        + "</div>"
    )

    # UTM source → submissions → invoice funnel.
    funnel = q.webform_funnel(clinic_id, days=days)
    sankey = _webform_funnel_sankey(funnel)

    body = (
        stats
        + '<div style="margin-top:18px;">' + sankey + "</div>"
    )
    if matched == 0:
        body += (
            '<p class="lede" style="margin-top:14px;color:var(--cx-mute);">'
            'No submitters matched a Blueprint patient — either Blueprint isn\'t '
            'connected for this clinic or contact details didn\'t line up.</p>'
        )
    return _section(4, title, lede, body)


def _trend_chart(
    months: list[str],
    series: list[tuple[str, list, str]],
    y_title: str,
    money: bool = False,
) -> str:
    """One Plotly line chart: ``series`` = [(name, values, color), …].

    Series that are entirely zero are dropped. Returns a stub if nothing is
    left to plot. ``money=True`` formats the y-axis as dollars.
    """
    active = [(n, v, c) for (n, v, c) in series if any(v)]
    if not active:
        return '<div class="empty">No data in the window.</div>'
    fig = go.Figure()
    for name, values, color in active:
        fig.add_trace(go.Scatter(
            x=months, y=values, name=name, mode="lines+markers",
            line=dict(color=color, width=2.5), marker=dict(size=6),
        ))
    fig.update_layout(
        paper_bgcolor=_CX_CREAM,
        plot_bgcolor=_CX_CREAM,
        font=dict(family="Geist, system-ui, sans-serif", color=_CX_NAVY, size=12),
        height=320,
        margin=dict(t=18, r=24, b=40, l=56),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        hovermode="x unified",
        xaxis=dict(showgrid=False),
        yaxis=dict(title=y_title, rangemode="tozero",
                   gridcolor="rgba(201,212,222,0.4)",
                   tickprefix="$" if money else ""),
    )
    return fig.to_html(
        include_plotlyjs="cdn", full_html=False,
        config={"displayModeBar": False},
    )


def _section_monthly_trends(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    google_ads_campaign_ids: list[str],
    days: int,
) -> str:
    """Monthly trends, split into four charts by stream and axis type.

    Everything is bucketed by acquisition-event month (see
    ``queries.monthly_trends``) so each month's leads sit with the bookings,
    invoices and revenue they eventually produced — a cohort view:

      1. Call stream volume   — calls, bookings, booking-attributed invoices
      2. Call stream revenue  — revenue from those booking-attributed invoices
      3. Web-form volume      — submissions, submissions with an associated booking
      4. Web-form revenue     — revenue attributed to submitters
    """
    lede = (
        "Month-over-month trends, bucketed by the month the lead came in. Calls, "
        "bookings, and the invoices/revenue they drove are attributed back to the "
        "call's month; web-form submissions, their associated bookings, and "
        "revenue to the submission's month."
    )
    rows = q.monthly_trends(
        clinic_id, invoca_campaign_ids, google_ads_campaign_ids, days=days,
    )
    if not rows:
        return _section(
            5, "Monthly trends", lede,
            '<div class="empty">No monthly data in the window.</div>',
        )

    def _label(mo: str) -> str:  # "2026-05" → "May 2026"
        y, m = mo.split("-")
        return f"{_dt.date(int(y), int(m), 1):%b %Y}"

    months = [_label(r["month"]) for r in rows]

    def col(key: str) -> list:
        return [r[key] for r in rows]

    def _block(title: str, chart: str) -> str:
        return (
            f'<div style="font-weight:600;color:var(--cx-navy);'
            f'margin:22px 0 6px;">{escape(title)}</div>{chart}'
        )

    chart1 = _trend_chart(months, [
        ("Inbound calls", col("calls"),         _CX_GOLD),
        ("Bookings",      col("bookings"),       _CX_GOLD_2),
        ("Invoices",      col("call_invoices"),  _CX_NAVY_3),
    ], "Count")
    chart2 = _trend_chart(months, [
        ("Call-stream revenue", col("call_revenue"), _CX_GREEN),
    ], "Revenue", money=True)
    chart3 = _trend_chart(months, [
        ("Web-form submissions", col("submissions"),      _CX_MOSS),
        ("Associated bookings",  col("webform_bookings"), _CX_GOLD_2),
    ], "Count")
    chart4 = _trend_chart(months, [
        ("Web-form revenue", col("webform_revenue"), _CX_GREEN),
    ], "Revenue", money=True)

    body = (
        _block("1 · Call stream — calls, bookings & attributed invoices", chart1)
        + _block("2 · Call stream — attributed revenue", chart2)
        + _block("3 · Web-form — submissions & associated bookings", chart3)
        + _block("4 · Web-form — attributed revenue", chart4)
    )
    return _section(5, "Monthly trends · by stream", lede, body)


def _section_roas(
    clinic_id: str,
    google_ads_campaign_ids: list[str],
    days: int,
) -> str:
    """Per-campaign Google Ads ROI cascade: clicks → calls → bookings, with
    spend/revenue/ROAS in each campaign's header.

    Reads :func:`queries.google_ads_roi` — spend from ``ad_groups``, clicks from
    ``ad_clicks_v2``, calls/bookings via GCLID → ``transactions`` →
    ``callscoring.appointment_booked``, and revenue from GCLID-matched callers
    phone-joined to Blueprint invoices. Everything here is campaign-level
    aggregate — no PHI — so it's safe on the pushed analytics surface. ROAS is
    revenue ÷ ad spend (the retainer is deliberately excluded; ads are judged on
    ad spend alone). Renders an explanatory stub when the clinic has no linked
    Google Ads campaigns or no clicks landed in the window.
    """
    lede = (
        "What the ad spend returned, per campaign. Spend buys clicks; clicks "
        "become tracked calls; calls become booked appointments; bookings tie "
        "back to Blueprint invoices for revenue. ROAS is revenue ÷ ad spend."
    )
    if not google_ads_campaign_ids:
        return _section(
            6, "Google Ads · return on ad spend", lede,
            '<div class="stub">No Google Ads campaigns are linked to this clinic '
            'yet — link them in <b>clinic_campaigns</b> to surface per-campaign '
            'ROAS.</div>',
        )
    rows = q.google_ads_roi(clinic_id, google_ads_campaign_ids, days=days)
    if not rows:
        return _section(
            6, "Google Ads · return on ad spend", lede,
            '<div class="stub">No Google Ads clicks landed in the window for the '
            'linked campaigns.</div>',
        )

    tot_spend   = sum(r["spend"] for r in rows)
    tot_revenue = sum(r["revenue"] for r in rows)
    blended_roas = (tot_revenue / tot_spend) if tot_spend else 0.0

    def _pct(v: float) -> str:
        return f"{v:.0f}%"

    def _roas(v: float) -> str:
        return f"{v:.1f}×"

    cards: list[str] = []
    for r in rows:
        summary = (
            f'spend <b>{_fmt_money(r["spend"])}</b> &nbsp;·&nbsp; '
            f'revenue <b>{_fmt_money(r["revenue"])}</b> &nbsp;·&nbsp; '
            f'ROAS <b>{_roas(r["roas"])}</b>'
        )
        stages = (
            '<div class="roi-stages">'
            '<div class="roi-stage" data-stage="clicks">'
            '<span class="label">Clicks</span>'
            f'<span class="value">{_fmt_int(r["clicks"])}</span>'
            f'<span class="sub">${r["cpc"]:,.2f}/click</span>'
            '</div>'
            '<div class="roi-arrow"><span class="arrow">→</span>'
            f'<span class="conv">{_pct(r["click_to_call_pct"])}</span>'
            f'<span class="cost">{_fmt_money(r["cost_per_call"])}/call</span>'
            '</div>'
            '<div class="roi-stage" data-stage="calls">'
            '<span class="label">Calls</span>'
            f'<span class="value">{_fmt_int(r["calls"])}</span>'
            '<span class="sub">tracked</span>'
            '</div>'
            '<div class="roi-arrow"><span class="arrow">→</span>'
            f'<span class="conv">{_pct(r["call_to_book_pct"])}</span>'
            f'<span class="cost">{_fmt_money(r["cost_per_booking"])}/booking</span>'
            '</div>'
            '<div class="roi-stage" data-stage="bookings">'
            '<span class="label">Bookings</span>'
            f'<span class="value">{_fmt_int(r["booked"])}</span>'
            f'<span class="sub">{_fmt_money(r["revenue_per_booking"])}/booking</span>'
            '</div>'
            '</div>'
        )
        cards.append(
            '<div class="roi-row">'
            '<div class="roi-header">'
            f'<span class="name">{escape(r["campaign_name"])}</span>'
            f'<span class="summary">{summary}</span>'
            '</div>'
            f'{stages}'
            '</div>'
        )

    blended = (
        '<div class="stub" style="margin-bottom:14px;">'
        f'Across linked campaigns: spend <b>{_fmt_money(tot_spend)}</b>, '
        f'revenue <b>{_fmt_money(tot_revenue)}</b>, '
        f'blended ROAS <b>{_roas(blended_roas)}</b>.'
        '</div>'
    )
    return _section(
        6, "Google Ads · return on ad spend", lede, blended + "".join(cards),
    )


def _section_callscoring(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int,
) -> str:
    """Per-flag distribution of LLM-scored calls — the raw classifier output.

    Six flags from ``ClinicData.callscoring`` rendered as a horizontal bar
    chart with count + share of scored calls. Flags overlap (a single call can
    be both ``existing_customer`` and ``appointment_booked``), so the bars do
    NOT sum to the total — the lede calls this out.
    """
    summary = q.callscoring_flag_summary(clinic_id, invoca_campaign_ids, days=days)
    total = summary["total_scored"]

    if not invoca_campaign_ids or total == 0:
        body = (
            '<div class="empty">No scored calls in the window — populates once '
            'transcripts have been processed by callscoring.</div>'
        )
        return _section(
            4, "Call categories",
            "How the LLM classified each scored call.",
            body,
        )

    stats = (
        '<div class="stats">'
        + _stat("Scored calls", _fmt_int(total), f"last {days}d")
        + "</div>"
    )

    bars = _bar_chart([
        (f"{f['label']}  · {(f['calls'] * 100 / total):.0f}%", f["calls"])
        for f in summary["flags"] if f["calls"] > 0
    ])

    body = (
        stats
        + '<div style="margin-top:18px;">' + bars + '</div>'
        + '<p class="lede" style="margin-top:14px;font-size:12px;">'
        '<i>Flags overlap — a single call can hit more than one (an existing '
        'customer can also book an appointment). Bars show raw flag counts, '
        'not mutually-exclusive buckets.</i></p>'
    )

    return _section(
        4, "Call categories",
        "How the LLM classified each scored call. Useful for triage: see what "
        "the inbound mix actually looks like beyond the funnel stages.",
        body,
    )


_COHORT_PREVIEW_LIMIT = 5


def _cohort_block(
    *,
    title: str,
    count: int,
    full_list_href: str,
    rows_html: str,
    head_html: str,
    empty_message: str,
) -> str:
    """One outer <details> cohort: banner summary + (head + 5 rows) + footer link.

    Caller is responsible for slicing rows_data to the preview limit and
    pre-rendering ``rows_html``. ``count`` is the total cohort size (used in the
    banner — may exceed the rows actually shown).
    """
    if count == 0:
        body = f'<div class="cx-cohort-empty">{escape(empty_message)}</div>'
    else:
        body = (
            '<div class="cx-cohort-body">'
            '<div class="cx-call-rows" style="border:none;border-radius:0;">'
            + head_html
            + rows_html
            + '</div>'
            '</div>'
            '<div class="cx-cohort-footer">'
            f'<a href="{full_list_href}" target="_top" class="cx-see-full">'
            f'See full list ({_fmt_int(count)} call{"" if count == 1 else "s"}) →'
            '</a>'
            '</div>'
        )
    return (
        '<details class="cx-cohort">'
        '  <summary>'
        '    <span class="cx-cohort-title">'
        f'      <span>{escape(title)}</span>'
        f'      <span class="cx-cohort-count">{_fmt_int(count)}</span>'
        '    </span>'
        '  </summary>'
        f'  {body}'
        '</details>'
    )


def _section_cohorts(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int,
    booking_window_hours: int = 24,
) -> str:
    """Three collapsible drill-down cohorts below the funnel: No Conversation,
    Qualified Lead — No Conv, and Attributed Invoices. Each shows the first 5
    rows when expanded with the same per-row layout as the standalone full
    detail page (including click-to-expand transcript dropdowns), plus a
    "See full list →" link to the dedicated report.
    """
    if not invoca_campaign_ids:
        return _section(
            5, "Cohorts · drill into the funnel",
            "Per-call detail for the three terminal branches of the funnel.",
            '<div class="empty">No Invoca campaigns linked — nothing to show.</div>',
        )

    # Pull only the preview rows per cohort (LIMIT pushed to SQL), plus a
    # separate COUNT(*) for the banner pill. Six small queries instead of
    # three potentially-large ones — net win once any cohort exceeds the
    # preview size. Six concurrent fetches because they're independent.
    from concurrent.futures import ThreadPoolExecutor as _Pool

    def _run(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            log.warning("cohorts subquery=%s clinic=%s failed: %s",
                        name, clinic_id, e)
            return None

    with _Pool(max_workers=6, thread_name_prefix="cohorts") as pool:
        futures = {
            "nc_rows":    pool.submit(_run, "nc_rows", lambda: q.no_conversation_detail(
                clinic_id, invoca_campaign_ids, days=days, limit=_COHORT_PREVIEW_LIMIT)),
            "nc_total":   pool.submit(_run, "nc_total", lambda: q.no_conversation_count(
                clinic_id, invoca_campaign_ids, days=days)),
            "qlnc_rows":  pool.submit(_run, "qlnc_rows", lambda: q.qualified_lead_no_conv_detail(
                clinic_id, invoca_campaign_ids, days=days, limit=_COHORT_PREVIEW_LIMIT)),
            "qlnc_total": pool.submit(_run, "qlnc_total", lambda: q.qualified_lead_no_conv_count(
                clinic_id, invoca_campaign_ids, days=days)),
            "inv_rows":   pool.submit(_run, "inv_rows", lambda: q.attributed_invoice_detail(
                clinic_id, invoca_campaign_ids,
                days=days, booking_window_hours=booking_window_hours,
                limit=_COHORT_PREVIEW_LIMIT)),
            "inv_total":  pool.submit(_run, "inv_total", lambda: q.attributed_invoice_count(
                clinic_id, invoca_campaign_ids,
                days=days, booking_window_hours=booking_window_hours)),
        }
        nc_preview   = futures["nc_rows"].result() or []
        nc_total     = futures["nc_total"].result() or 0
        qlnc_preview = futures["qlnc_rows"].result() or []
        qlnc_total   = futures["qlnc_total"].result() or 0
        inv_preview  = futures["inv_rows"].result() or []
        inv_total    = futures["inv_total"].result() or 0

    # Single transcript batch for all three previews. Dedup naturally.
    from intelligence_report.transcripts import get_transcripts
    ccids = (
        [r.get("complete_call_id") for r in nc_preview]
        + [r.get("complete_call_id") for r in qlnc_preview]
        + [r.get("first_call_id")    for r in inv_preview]
    )
    try:
        transcripts = get_transcripts([c for c in ccids if c])
    except Exception as e:  # noqa: BLE001
        log.warning("transcripts unavailable for clinic_id=%s: %s", clinic_id, e)
        transcripts = {}

    # Render preview rows for each cohort.
    nc_rows_html = "".join(
        _render_outcome_row(r, transcripts.get(r.get("complete_call_id") or ""))
        for r in nc_preview
    )
    qlnc_rows_html = "".join(
        _render_outcome_row(r, transcripts.get(r.get("complete_call_id") or ""))
        for r in qlnc_preview
    )

    # Invoice rows need pre-formatted transcript HTML + per-patient bands.
    rendered_inv_transcripts: dict[str, str] = {}
    inv_rows_html_parts: list[str] = []
    last_client_id = None
    band = 0
    for r in inv_preview:
        if r["client_id"] != last_client_id:
            band ^= 1
            last_client_id = r["client_id"]
        ccid = r.get("first_call_id") or ""
        if ccid not in rendered_inv_transcripts:
            rendered_inv_transcripts[ccid] = _format_transcript(transcripts.get(ccid))
        inv_rows_html_parts.append(
            _render_invoice_row(r, rendered_inv_transcripts[ccid], band)
        )
    inv_rows_html = "".join(inv_rows_html_parts)

    cohorts_html = (
        _CALL_ROWS_CSS
        + _INVOICE_EXTRA_CSS
        + _COHORT_CSS
        + _cohort_block(
            title="No Conversation",
            count=nc_total,
            full_list_href=f"/intelligence/{clinic_id}/no-conversation-calls",
            rows_html=nc_rows_html,
            head_html=_outcome_head_html(),
            empty_message="No calls hit this bucket in the window.",
        )
        + _cohort_block(
            title="Qualified Lead — No Conversion",
            count=qlnc_total,
            full_list_href=f"/intelligence/{clinic_id}/qualified-no-conv-calls",
            rows_html=qlnc_rows_html,
            head_html=_outcome_head_html(),
            empty_message="No calls hit this bucket in the window.",
        )
        + _cohort_block(
            title="Attributed Invoices",
            count=inv_total,
            full_list_href=f"/intelligence/{clinic_id}/attributed-invoices",
            rows_html=inv_rows_html,
            head_html=_invoice_head_html(),
            empty_message="No marketing-attributed invoices in the window.",
        )
    )

    return _section(
        5, "Cohorts · drill into the funnel",
        "Per-call detail for the three terminal branches of the funnel. Click "
        "any cohort to expand the first few rows, then click a row to read the "
        "transcript. Use \"See full list\" to view every row.",
        cohorts_html,
    )


def _detail_button(href: str, label: str, *, enabled: bool = True) -> str:
    """Inline button that links (with target=_top) to a detail page, or
    renders disabled when there's nothing to drill into."""
    if not enabled:
        return (
            f'<span style="display:inline-block;background:var(--cx-cream-2);'
            f'color:var(--cx-mute);font-family:Geist Mono,monospace;font-size:11px;'
            f'text-transform:uppercase;letter-spacing:0.08em;padding:10px 16px;'
            f'border-radius:4px;border:1px solid var(--cx-rule);">'
            f'{escape(label)}</span>'
        )
    return (
        f'<a href="{href}" target="_top" '
        f'   style="display:inline-block;background:var(--cx-navy);color:var(--cx-cream);'
        f'          font-family:Geist Mono,monospace;font-size:11px;text-transform:uppercase;'
        f'          letter-spacing:0.08em;padding:10px 16px;border-radius:4px;text-decoration:none;'
        f'          border:1px solid var(--cx-navy-3);">'
        f'  {escape(label)}'
        f'</a>'
    )


def _section_engagement(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int,
) -> str:
    """Engagement · heuristic spam classification used to filter §03's funnel."""
    s = q.spam_calls_summary(clinic_id, invoca_campaign_ids, days=days)

    stats = (
        '<div class="stats">'
        + _stat(
            "Spam-classified calls",
            _fmt_int(s["spam_calls"]),
            f"{s['spam_pct']:.1f}% of {_fmt_int(s['total_calls'])} total",
        )
        + _stat(
            "Total inbound",
            _fmt_int(s["total_calls"]),
            f"last {days}d",
        )
        + "</div>"
    )

    link = ""
    if s["spam_calls"] > 0:
        link = (
            f'<div style="margin-top:18px;">'
            f'  <a href="/intelligence/{escape(clinic_id)}/spam-calls" target="_top"'
            f'     style="display:inline-block;background:var(--cx-navy);color:var(--cx-cream);'
            f'            font-family:Geist Mono,monospace;font-size:11px;text-transform:uppercase;'
            f'            letter-spacing:0.08em;padding:10px 16px;border-radius:4px;text-decoration:none;'
            f'            border:1px solid var(--cx-navy-3);">'
            f'    View spam-call line items →'
            f'  </a>'
            f'</div>'
        )

    return _section(
        2,
        "Engagement · spam",
        "Calls our LLM classified as spam or solicitor. These are filtered out "
        "of §03's funnel upstream so the patient-acquisition view operates on "
        "real inbound traffic only. The classifier reads each call transcript "
        "and flags B2B solicitors, robocalls, autodialer junk, and other "
        "non-patient-care interactions — see each row's reasoning on the spam "
        "detail page.",
        stats + link,
    )


def _section_executive(clinic_id: str, days: int) -> str:
    appts = q.appointment_outcomes(clinic_id, days=days)
    rev   = q.invoice_revenue(clinic_id, days=days)

    completed = appts["by_status"].get("Completed", 0) + appts["by_status"].get("Arrived", 0)
    cancelled = appts["by_status"].get("Cancelled", 0)
    no_show   = appts["by_status"].get("No show", 0)
    booked    = appts["by_status"].get("Confirmed", 0) + appts["by_status"].get("Tentative", 0)
    sales_opp = appts["sales_opportunities"]
    avg_inv   = (rev["revenue"] / rev["invoice_count"]) if rev["invoice_count"] else 0

    stats = (
        '<div class="stats">'
        + _stat("Total revenue", _fmt_money(rev["revenue"]),
                f"{_fmt_int(rev['invoice_count'])} invoices · last {days}d")
        + _stat("Avg invoice", _fmt_money(avg_inv))
        + _stat("Appointments",   _fmt_int(appts["total"]),
                f"sales opp: {_fmt_int(sales_opp)}")
        + _stat("Completed",      _fmt_int(completed))
        + _stat("Cancelled",      _fmt_int(cancelled))
        + _stat("No-shows",       _fmt_int(no_show))
        + _stat("Booked (future)", _fmt_int(booked))
        + "</div>"
    )
    return _section(
        1, "Executive summary",
        f"Practice activity over the last <b>{days}</b> days, drawn from the Blueprint daily snapshot. "
        "Revenue is the sum of <i>order total (with tax)</i> across invoices with a non-zero total. "
        "Appointment statuses are Blueprint's own bucketing — 'Completed' + 'Arrived' both count as a real visit.",
        stats,
    )


def _section_referrals(clinic_id: str, days: int) -> str:
    rows = q.referral_breakdown(clinic_id, days=days, top_n=10)
    total = sum(r["revenue"] for r in rows) or 1.0
    table_rows = [
        [
            escape(r["source_name"]),
            f"{escape(r['source_type'])}",
            _fmt_int(r["invoice_count"]),
            _fmt_money(r["revenue"]),
            f"{r['revenue'] / total * 100:.1f}%",
        ]
        for r in rows
    ]
    return _section(
        2, "Top referral sources",
        f"Where invoiced revenue came from over the last <b>{days}</b> days. "
        "Sources are joined from <code>InvoiceMaster.referrer_type_id</code> + "
        "<code>referral_source_id</code> against the clinic's own ReferralSources table.",
        _table(
            ["Source", "Type", "Invoices", "Revenue", "% of top 10"],
            table_rows,
        ),
    )


def _section_patient_product(clinic_id: str, days: int) -> str:
    patients = q.patient_demographics(clinic_id)
    line_mix = q.line_item_mix(clinic_id, days=days)

    patient_bars = _bar_chart(sorted(patients.items(), key=lambda kv: -kv[1])[:8])
    body = (
        '<h3 style="font-family: Lora, serif; font-weight: 500; font-size: 16px; margin: 6px 0 8px; color: var(--cx-navy);">Patient status mix</h3>'
        + patient_bars
    )

    # Line-item revenue split only renders when the PMS supplies line items
    # (Blueprint does; CounselEar invoices have no line detail) — otherwise the
    # patient-status block stands alone rather than showing an empty table.
    if line_mix:
        total_lines = sum(r["line_count"] for r in line_mix) or 1
        line_rows = [
            [
                escape(r["item_type"]),
                _fmt_int(r["line_count"]),
                f"{r['line_count'] / total_lines * 100:.1f}%",
                _fmt_money(r["revenue"]),
            ]
            for r in line_mix
        ]
        body += (
            '<h3 style="font-family: Lora, serif; font-weight: 500; font-size: 16px; margin: 22px 0 8px; color: var(--cx-navy);">Line-item revenue split</h3>'
            + _table(["Item type", "Lines", "% lines", "Revenue"], line_rows)
        )

    title = "Patient & product mix" if line_mix else "Patient mix"
    lede = (
        "Patient base by PMS status"
        + ("; line-item revenue broken out by <code>item_type</code> "
           "(hearing aid, accessory, service, etc.)." if line_mix else ".")
    )
    return _section(3, title, lede, body)


# ── Top-level ────────────────────────────────────────────────────────────────

def generate_report(clinic_id: str, clinic_name: str, days: int = 365) -> str:
    """Legacy entry point — used by tests / one-off CLI calls that don't have
    campaign IDs. The route handler uses :func:`generate_report_with_campaigns`
    instead. Both share the same body now; this overload just passes empty
    campaign lists, so the Acquisition section renders zeros.
    """
    return generate_report_with_campaigns(
        clinic_id=clinic_id,
        clinic_name=clinic_name,
        invoca_campaign_ids=[],
        google_ads_campaign_ids=[],
        days=days,
    )


# Canonical date-range windows (days). The pre-warm job (prewarm.py) renders
# these for every active clinic so the standard ranges are always cache-warm;
# the UI's range presets should match this list for guaranteed hits. Arbitrary
# windows still render live (a cache miss) — they're just not pre-warmed.
CANONICAL_RANGES = [30, 90, 180, 365, 730]

# ── Rendered-report cache ─────────────────────────────────────────────────────
# The full-report cache key encodes a data_version (Blueprint snapshot date +
# UTC date — see generate_report_with_campaigns), so a hit is valid by
# construction and the TTL can be long: new data → new key, never a stale serve.
# 6h keeps the standard ranges warm through a working day on a single instance.
# Bounded size; GIL serialises the dict mutations.
_REPORT_CACHE_TTL_SECONDS = 21600  # 6h — safe because the key is data-versioned
_REPORT_CACHE_MAX_ENTRIES = 128
_REPORT_CACHE: dict[tuple, tuple[float, str]] = {}

# Per-section cache. Its key is NOT data-versioned, so keep the original short
# TTL — a filter that only affects one section (e.g. the §03 UTM source) reuses
# the other sections' cached HTML instead of re-running their BigQuery queries.
_SECTION_CACHE_TTL_SECONDS = 300
_SECTION_CACHE_MAX_ENTRIES = 512
_SECTION_CACHE: dict[tuple, tuple[float, str]] = {}


def _section_cache_get(key: tuple) -> str | None:
    import time as _time
    entry = _SECTION_CACHE.get(key)
    if entry is None:
        return None
    ts, html = entry
    if _time.time() - ts > _SECTION_CACHE_TTL_SECONDS:
        _SECTION_CACHE.pop(key, None)
        return None
    return html


def _section_cache_put(key: tuple, html: str) -> None:
    import time as _time
    if len(_SECTION_CACHE) >= _SECTION_CACHE_MAX_ENTRIES:
        oldest = min(_SECTION_CACHE, key=lambda k: _SECTION_CACHE[k][0])
        _SECTION_CACHE.pop(oldest, None)
    _SECTION_CACHE[key] = (_time.time(), html)


def _report_cache_key(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    google_ads_campaign_ids: list[str],
    days: int,
    utm_sources: list[str] | None = None,
    utm_mediums: list[str] | None = None,
    data_version: str = "",
) -> tuple:
    return (
        clinic_id,
        int(days),
        tuple(sorted(invoca_campaign_ids)),
        tuple(sorted(google_ads_campaign_ids)),
        tuple(sorted(utm_sources or [])),
        tuple(sorted(utm_mediums or [])),
        data_version,
    )


def _report_cache_get(key: tuple) -> str | None:
    import time as _time
    entry = _REPORT_CACHE.get(key)
    if entry is None:
        return None
    ts, html = entry
    if _time.time() - ts > _REPORT_CACHE_TTL_SECONDS:
        # Expired — drop and miss.
        _REPORT_CACHE.pop(key, None)
        return None
    return html


def _report_cache_put(key: tuple, html: str) -> None:
    import time as _time
    # Evict oldest entries when full. O(N) but N is tiny so it's fine.
    if len(_REPORT_CACHE) >= _REPORT_CACHE_MAX_ENTRIES:
        oldest = min(_REPORT_CACHE, key=lambda k: _REPORT_CACHE[k][0])
        _REPORT_CACHE.pop(oldest, None)
    _REPORT_CACHE[key] = (_time.time(), html)


# ── Shared, persistent report cache (GCS-backed) ──────────────────────────────
# The in-process caches above are per-instance and lost on cold start / scale-out.
# This layer is shared across instances and survives restarts, and lets the
# pre-warm job (intelligence_report/prewarm.py) populate the cache after each ETL
# load so interactive loads of the standard ranges are near-instant. The cache key
# encodes a data_version (Blueprint snapshot date + UTC date), so an object's mere
# existence implies it's valid — stale data yields a different key, so no TTL is
# needed (the bucket also has a 14-day lifecycle to sweep orphaned versions). A GCS
# GET is ~100 ms (vs ~3 s for a BigQuery point-lookup), so warm hits are
# sub-second. Every op fails safe (None / no-op) so a storage hiccup never breaks
# rendering. The cached payload is the §00–§06 analytics surface (aggregate-only,
# no patient PII).
#
# The bucket is dedicated, private, and lifecycle-managed; the hypervisor SA has
# roles/storage.objectAdmin on it. To repoint elsewhere, change the constant and
# grant the SA objectAdmin on the new bucket.
_REPORT_CACHE_BUCKET = "project-demo-2-482101-report-cache"
_REPORT_CACHE_PREFIX = "report-cache/"
_storage_client = None  # lazy singleton


def _cache_key_str(key: tuple) -> str:
    import hashlib
    return hashlib.sha256(repr(key).encode()).hexdigest()


# Snapshot date is needed on every request to form the data_version, but it only
# changes when ETL runs (≈daily). Querying BigQuery for it on each hit was the
# dominant cost on the otherwise ~100 ms GCS warm path, so memoize it per clinic
# with a short TTL — a stale read is bounded by the TTL and the data_version also
# carries the UTC date as a backstop.
_SNAPSHOT_CACHE: dict[str, tuple[float, object]] = {}
_SNAPSHOT_TTL_SECONDS = 600


def _cached_snapshot_date(clinic_id: str):
    import time as _time
    entry = _SNAPSHOT_CACHE.get(clinic_id)
    if entry is not None and _time.time() - entry[0] <= _SNAPSHOT_TTL_SECONDS:
        return entry[1]
    val = q.blueprint_snapshot_date(clinic_id)
    _SNAPSHOT_CACHE[clinic_id] = (_time.time(), val)
    return val


def _cache_bucket():
    global _storage_client
    if _storage_client is None:
        from google.cloud import storage
        _storage_client = storage.Client()
    return _storage_client.bucket(_REPORT_CACHE_BUCKET)


def _cache_blob_name(key_str: str, clinic_id: str) -> str:
    # clinic_id in the path keeps objects browsable/cleanable per clinic; key_str
    # (a sha256 of the full data-versioned key) guarantees uniqueness.
    return f"{_REPORT_CACHE_PREFIX}{clinic_id}/{key_str}.html"


def _shared_cache_get(key_str: str, clinic_id: str) -> str | None:
    from google.cloud.exceptions import NotFound
    try:
        return _cache_bucket().blob(
            _cache_blob_name(key_str, clinic_id)).download_as_text()
    except NotFound:
        return None
    except Exception as exc:  # never let the cache path break rendering
        log.warning("shared report cache GET failed: %s", exc)
        return None


def _shared_cache_put(key_str: str, clinic_id: str, html: str, data_version: str) -> None:
    try:
        blob = _cache_bucket().blob(_cache_blob_name(key_str, clinic_id))
        blob.metadata = {"data_version": data_version, "clinic_id": clinic_id}
        blob.upload_from_string(html, content_type="text/html; charset=utf-8")
    except Exception as exc:  # best-effort; a write failure must not break serving
        log.warning("shared report cache PUT failed: %s", exc)


def generate_report_with_campaigns(
    clinic_id: str,
    clinic_name: str,
    invoca_campaign_ids: list[str],
    google_ads_campaign_ids: list[str] | None = None,
    days: int = 365,
    use_cache: bool = True,
    utm_sources: list[str] | None = None,
    utm_mediums: list[str] | None = None,
) -> str:
    """Render the full report scoped to a clinic's linked campaigns.

    Marketing sections (inbound calls, end-to-end funnel, Google Ads ROI)
    take the linked campaign IDs as args so this module doesn't depend on the
    Cloud SQL ORM. The hypervisor route looks the IDs up and passes them in.

    ``utm_sources`` / ``utm_mediums`` are multi-select include-lists that filter
    the §03 call funnel (source AND medium; each optional); they only affect §03.

    Set ``use_cache=False`` to bypass the in-process TTL cache (useful for
    debugging / verifying a fresh render).
    """
    google_ads_campaign_ids = google_ads_campaign_ids or []
    utm_sources = q._utm_clean(utm_sources)
    utm_mediums = q._utm_clean(utm_mediums)

    # Data version: a cached render is only valid for the same underlying data.
    # Fold the Blueprint snapshot date + UTC date into the cache key so a new ETL
    # load (or a new day) yields a fresh key instead of serving stale HTML — this
    # is what lets the cache TTLs be long. snapshot/today are reused in the header.
    snapshot = _cached_snapshot_date(clinic_id)
    today    = _dt.date.today().isoformat()
    data_version = f"{snapshot}|{today}"

    cache_key = _report_cache_key(
        clinic_id, invoca_campaign_ids, google_ads_campaign_ids, days,
        utm_sources, utm_mediums, data_version,
    )
    key_str = _cache_key_str(cache_key)
    if use_cache:
        cached = _report_cache_get(cache_key)
        if cached is not None:
            log.info("intelligence_report cache=HIT(mem) clinic=%s days=%d", clinic_id, days)
            return cached
        cached = _shared_cache_get(key_str, clinic_id)
        if cached is not None:
            _report_cache_put(cache_key, cached)  # promote into the in-process tier
            log.info("intelligence_report cache=HIT(shared) clinic=%s days=%d", clinic_id, days)
            return cached
        log.info("intelligence_report cache=MISS clinic=%s days=%d", clinic_id, days)

    header = (
        f'<header class="report-header">'
        f'  <div class="eyebrow">CORTEX · clinic intelligence</div>'
        f'  <h1>{escape(clinic_name)}</h1>'
        f'  <div class="meta">'
        + (
            f'    Blueprint snapshot: <b style="color:var(--cx-cream)">{escape(str(snapshot))}</b> &nbsp;·&nbsp;'
            if snapshot else ''
        )
        + f'    Generated {today} &nbsp;·&nbsp; Window: {days}d'
        + f'  </div>'
          f'</header>'
    )

    # Per-section timing + exception isolation. Sections are run concurrently
    # in a thread pool — they're independent (each kicks off its own BQ
    # queries) and BigQuery/GCS clients are thread-safe, so total wall time
    # drops to max(section_time) instead of sum. Order is preserved by
    # ThreadPoolExecutor.map.
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    inv_key = tuple(sorted(invoca_campaign_ids))
    ga_key = tuple(sorted(google_ads_campaign_ids))

    def _timed(name: str, fn) -> str:
        # Only §03 varies with utm_source; everything else is shared across
        # filter values, so a UTM change reuses their cached HTML.
        if name == "00_headline":
            # Month-over-month: independent of the day window / utm filter.
            sect_key = (name, clinic_id, inv_key)
        elif name == "03_funnel":
            sect_key = (name, clinic_id, int(days), inv_key, ga_key,
                        tuple(sorted(utm_sources)), tuple(sorted(utm_mediums)))
        else:
            sect_key = (name, clinic_id, int(days), inv_key, ga_key)
        cached = _section_cache_get(sect_key) if use_cache else None
        if cached is not None:
            log.info("intelligence_report section=%s clinic=%s cache=HIT", name, clinic_id)
            return cached

        t0 = _time.perf_counter()
        try:
            html = fn()
            dt = _time.perf_counter() - t0
            log.info("intelligence_report section=%s clinic=%s ok dt=%.2fs",
                     name, clinic_id, dt)
            if use_cache:
                _section_cache_put(sect_key, html)
            return html
        except Exception as e:
            dt = _time.perf_counter() - t0
            log.exception("intelligence_report section=%s clinic=%s FAILED dt=%.2fs",
                          name, clinic_id, dt)
            return (
                f'<section><h2><span class="num">--</span>{escape(name)} unavailable</h2>'
                f'<p class="lede">This section failed to render: <code>{escape(type(e).__name__)}: {escape(str(e)[:200])}</code></p></section>'
            )

    # This report is the "Analytics" surface: marketing analytics only —
    # acquisition, engagement, funnel, ROAS. The call-level breakdowns
    # (callscoring categories, cohort drill-downs) moved to the separate
    # "Leads" section (React + JSON via api/worklists.py). The
    # _section_callscoring / _section_cohorts functions are intentionally kept
    # — they still power the standalone detail HTML pages (no-conversation,
    # qualified-no-conv, attributed-invoices) served by api/intelligence.py.
    section_specs: list[tuple[str, callable]] = [
        ("00_headline",       lambda: _section_headline(
            clinic_id, clinic_name, invoca_campaign_ids)),
        ("01_acquisition",    lambda: _section_acquisition(
            clinic_id, invoca_campaign_ids, google_ads_campaign_ids, days=days)),
        ("02_engagement",     lambda: _section_engagement(
            clinic_id, invoca_campaign_ids, days=days)),
        ("03_funnel",         lambda: _section_funnel(
            clinic_id, invoca_campaign_ids, google_ads_campaign_ids, days=days,
            utm_sources=utm_sources, utm_mediums=utm_mediums)),
        ("04_webform_revenue", lambda: _section_webform_revenue(
            clinic_id, days=days)),
        ("05_monthly_trends", lambda: _section_monthly_trends(
            clinic_id, invoca_campaign_ids, google_ads_campaign_ids, days=days)),
        ("06_roas",           lambda: _section_roas(
            clinic_id, google_ads_campaign_ids, days=days)),
    ]

    t_start = _time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(section_specs),
                            thread_name_prefix="intel_sections") as pool:
        section_html = list(pool.map(lambda spec: _timed(*spec), section_specs))
    log.info("intelligence_report TOTAL clinic=%s days=%d wall=%.2fs",
             clinic_id, days, _time.perf_counter() - t_start)

    body = (
        header
        + "".join(section_html)
        + f'<footer>Built by CORTEX · {today} · clinic_id <code>{escape(clinic_id)}</code></footer>'
    )

    title = f"{clinic_name} — Intelligence · CORTEX"
    html = _HEAD.format(title=escape(title)) + body + _FOOT
    if use_cache:
        _report_cache_put(cache_key, html)
        _shared_cache_put(key_str, clinic_id, html, data_version)
    return html


def _fmt_dt(v) -> str:
    """Format a datetime/date/string consistently. Empty when None."""
    if v is None or v == "":
        return ""
    s = str(v)
    # Trim trailing time-zone/microsecond noise — '2026-04-15 14:30:00+00:00'
    # is more readable than the BQ default repr.
    if "+" in s:
        s = s.split("+", 1)[0].rstrip()
    if "." in s and " " in s:
        s = s.split(".", 1)[0]
    if "T" in s:
        s = s.replace("T", " ")
    return s


def _fmt_phone(raw) -> str:
    """Display a phone as (xxx) xxx-xxxx; passthrough on anything weird."""
    if not raw:
        return ""
    s = str(raw)
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return s


def generate_attributed_invoices_report(
    clinic_id: str,
    clinic_name: str,
    invoca_campaign_ids: list[str],
    days: int = 365,
    booking_window_hours: int = 24,
) -> str:
    """Render a standalone HTML page listing every invoice that ties back to a
    tracked phone-campaign call.

    One row per (call × invoice) — a patient who called twice and bought once
    appears as two rows. PHI (name, client_id, phone) is rendered as-is; the
    endpoint that serves this is gated to admin / super_admin.
    """
    snapshot = q.blueprint_snapshot_date(clinic_id)
    today    = _dt.date.today().isoformat()

    rows_data = q.attributed_invoice_detail(
        clinic_id, invoca_campaign_ids,
        days=days, booking_window_hours=booking_window_hours,
    )

    total_revenue = sum(r["order_total"] for r in rows_data)
    unique_invoices = len({r["invoice_order_id"] for r in rows_data})
    unique_patients = len({r["client_id"] for r in rows_data})
    avg_per_patient = (total_revenue / unique_patients) if unique_patients else 0

    # Back link → main report. target="_top" breaks out of the parent iframe
    # if this page is itself wrapped in one.
    back_btn = (
        f'<a href="/intelligence/{escape(clinic_id)}" target="_top" '
        f'   style="color:var(--cx-gold);text-decoration:none;font-family:Geist Mono,monospace;'
        f'          font-size:11px;text-transform:uppercase;letter-spacing:0.08em;">'
        f'  ← Back to intelligence report'
        f'</a>'
    )

    header = (
        f'<header class="report-header">'
        f'  <div class="eyebrow">CORTEX · marketing revenue</div>'
        f'  <h1>{escape(clinic_name)}</h1>'
        f'  <div class="meta">'
        + (
            f'    Blueprint snapshot: <b style="color:var(--cx-cream)">{escape(str(snapshot))}</b> &nbsp;·&nbsp;'
            if snapshot else ''
        )
        + f'    Generated {today} &nbsp;·&nbsp; Window: {days}d &nbsp;·&nbsp;'
          f'    Booking window: {booking_window_hours}h'
          f'  </div>'
          f'  <div style="margin-top:14px;">{back_btn}</div>'
          f'</header>'
    )

    if not rows_data:
        body = (
            header
            + '<section>'
            + '<h2><span class="num">--</span>No marketing-acquired patients with invoices yet</h2>'
            + '<p class="lede">No patient in the last '
            + f'<b>{days}</b> days has both (a) been acquired through a tracked phone call from a linked Invoca campaign with an appointment booked within '
            + f'<b>{booking_window_hours}h</b> of that call, and (b) generated an invoice on or after that appointment. '
            + 'Either no Invoca campaigns are linked, or the chain hasn&apos;t produced revenue yet in this window.</p>'
            + '</section>'
        )
        title = f"{clinic_name} — Marketing revenue · CORTEX"
        return _HEAD.format(title=escape(title)) + body + _FOOT

    summary = (
        '<div class="stats">'
        + _stat("Marketing-touched patients", _fmt_int(unique_patients))
        + _stat("Invoices",                   _fmt_int(unique_invoices))
        + _stat("Total spent",                _fmt_money(total_revenue),
                "matches §02 funnel revenue")
        + _stat("Avg per patient",            _fmt_money(avg_per_patient))
        + "</div>"
    )

    # Fetch transcripts once per unique first-call so a patient with multiple
    # invoices doesn't trigger multiple GCS reads.
    from intelligence_report.transcripts import get_transcripts
    unique_call_ids = {r["first_call_id"] for r in rows_data if r.get("first_call_id")}
    try:
        transcripts = get_transcripts(list(unique_call_ids))
    except Exception as e:  # noqa: BLE001
        log.warning("transcripts unavailable for clinic_id=%s: %s", clinic_id, e)
        transcripts = {}

    # One row per (patient × invoice). The acquisition columns repeat for each
    # invoice the same patient generated; the transcript dropdown is the SAME
    # first-call transcript on every row for a patient (rendered once, reused).
    # Cache the formatted transcript HTML per first_call_id so we don't re-run
    # the formatter for every invoice of the same patient.
    rendered_transcripts: dict[str, str] = {}
    rows_html: list[str] = []
    last_client_id = None
    band = 0
    for r in rows_data:
        if r["client_id"] != last_client_id:
            band ^= 1
            last_client_id = r["client_id"]
        ccid = r.get("first_call_id") or ""
        if ccid not in rendered_transcripts:
            rendered_transcripts[ccid] = _format_transcript(transcripts.get(ccid))
        rows_html.append(_render_invoice_row(r, rendered_transcripts[ccid], band))

    total_row = (
        '<div class="cx-call-row-total">'
        '<span>Total spent by marketing-acquired patients</span>'
        f'<span class="num">{_fmt_money(total_revenue)}</span>'
        '</div>'
    )
    table = (
        _CALL_ROWS_CSS
        + _INVOICE_EXTRA_CSS
        + '<div class="cx-call-rows">'
        + _invoice_head_html()
        + "".join(rows_html)
        + total_row
        + '</div>'
    )

    section_html = (
        '<section>'
        '<h2><span class="num">01</span>Money spent by marketing-touched patients</h2>'
        '<p class="lede">'
        f'Every patient whose phone matched a tracked Invoca call in the last <b>{days}</b> days, '
        f'and all the invoices that patient generated in the same window — the same loose '
        'attribution used in §02 Revenue funnel\'s <i>Matched revenue</i> stat. One row per '
        '(patient × invoice), so a patient with three invoices shows up three times. '
        f'The <b>First appt</b> column shows the earliest appointment booked within '
        f'<b>{booking_window_hours}h</b> of the first call when one exists, otherwise — '
        '(the patient was reached but didn\'t book in-window; their later invoices still '
        'count because they were touched by marketing). Sort: most recently touched patient '
        'first; invoices chronological within each patient.'
        '</p>'
        + summary + table
        + '</section>'
    )

    body = (
        header
        + section_html
        + f'<footer>Built by CORTEX · {today} · clinic_id <code>{escape(clinic_id)}</code></footer>'
    )
    title = f"{clinic_name} — Marketing revenue · CORTEX"
    return _HEAD.format(title=escape(title)) + body + _FOOT


def _fmt_duration(seconds: int) -> str:
    """Display call duration as ``M:SS`` (e.g. ``0:04`` for a 4-second ring)."""
    s = max(int(seconds or 0), 0)
    return f"{s // 60}:{s % 60:02d}"


def _format_transcript(transcript) -> str:
    """Render an Invoca transcript JSON blob into an HTML fragment.

    Best-effort rendering across the shapes Invoca's
    ``caller_agent_conversation`` endpoint returns:
      - top-level list of turns ``[{speaker, text, ...}, ...]``
      - dict wrapper ``{"transcript": [...]}`` or ``{"turns": [...]}``
      - dict with a ``text`` field (single-shot transcript)
    Falls back to a pretty-printed JSON ``<pre>`` for anything unrecognized so
    nothing is silently dropped.
    """
    if transcript is None:
        return '<p class="cx-transcript-empty">No transcript available for this call.</p>'

    turns = transcript
    if isinstance(transcript, dict):
        turns = (
            transcript.get("transcript")
            or transcript.get("turns")
            or transcript.get("conversation")
            or transcript.get("messages")
        )
        if not turns and transcript.get("text"):
            return (
                '<div class="cx-transcript-turns">'
                f'<div class="cx-turn"><span class="cx-speaker">Transcript:</span> '
                f'{escape(str(transcript["text"]))}</div></div>'
            )

    if isinstance(turns, list) and turns and isinstance(turns[0], dict):
        rows: list[str] = []
        for turn in turns:
            # Invoca's `caller_agent_conversation` shape: each turn is exactly
            # one of {"agent": "..."} or {"caller": "..."}.
            if "agent" in turn and isinstance(turn["agent"], str):
                speaker, text = "Agent", turn["agent"]
            elif "caller" in turn and isinstance(turn["caller"], str):
                speaker, text = "Caller", turn["caller"]
            else:
                # Fall back to generic speaker/text key detection.
                speaker = (
                    turn.get("speaker")
                    or turn.get("party")
                    or turn.get("role")
                    or turn.get("name")
                    or ""
                )
                text = (
                    turn.get("text")
                    or turn.get("transcript")
                    or turn.get("utterance")
                    or turn.get("content")
                    or ""
                )
            if not (speaker or text):
                continue
            sp_class = "cx-speaker-agent" if speaker == "Agent" else (
                "cx-speaker-caller" if speaker == "Caller" else "cx-speaker"
            )
            sp = escape(str(speaker)).strip()
            tx = escape(str(text)).strip()
            rows.append(
                f'<div class="cx-turn">'
                f'  <span class="cx-speaker {sp_class}">{sp}{":" if sp else ""}</span> '
                f'  <span class="cx-text">{tx}</span>'
                f'</div>'
            )
        if rows:
            return '<div class="cx-transcript-turns">' + "".join(rows) + '</div>'

    # Fallback — show the raw JSON so nothing is hidden, just not pretty.
    import json as _json
    return (
        '<pre class="cx-transcript-raw">'
        + escape(_json.dumps(transcript, indent=2, default=str))
        + '</pre>'
    )


# Inline CSS for the per-call <details> rows on line-item pages. Native
# <details>/<summary> — no JS, so the iframe sandbox doesn't matter.
_CALL_ROWS_CSS = """
<style>
.cx-call-rows { display: flex; flex-direction: column;
  border: 1px solid var(--cx-rule); border-radius: 4px; background: white; }
.cx-call-row-head, .cx-call-row > summary {
  display: grid;
  /* Default (spam) layout: 5 data columns + chevron column. The last `auto`
     track holds the chevron span so every row across head/body has the same
     column count and the data columns can't shift. */
  grid-template-columns: 1.4fr 1.4fr 0.7fr 0.7fr 2fr 20px;
  gap: 12px; padding: 10px 14px; align-items: center;
}
.cx-call-row-head.cx-row-6, .cx-call-row > summary.cx-row-6 {
  /* Description · Timestamp · Phone · Connect · Duration · UTM medium · chevron */
  grid-template-columns: 2.5fr 1.3fr 1.3fr 0.6fr 0.6fr 1fr 20px;
}
.cx-call-row-head.cx-row-9, .cx-call-row > summary.cx-row-9 {
  /* Patient · Patient ID · Total · Inv date · Inv # · First call · First appt · UTM source · UTM medium · chevron */
  grid-template-columns: 1.3fr 1fr 0.8fr 0.9fr 0.9fr 1.1fr 1.1fr 1fr 0.8fr 20px;
  font-size: 12px;
}
.cx-call-row-head {
  font-family: Geist Mono, monospace; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--cx-mute); border-bottom: 1px solid var(--cx-rule);
}
.cx-call-row { border-bottom: 1px solid var(--cx-rule); }
.cx-call-row:last-child { border-bottom: none; }
.cx-call-row > summary {
  cursor: pointer; list-style: none; user-select: none;
  font-size: 13px;
}
.cx-call-row > summary::-webkit-details-marker { display: none; }
/* Chevron is the LAST explicit grid cell (a span the renderer emits as
   `<span class="cx-chevron">`). Head rows emit an empty chevron span too so
   the column tracks match exactly. */
.cx-chevron { color: var(--cx-mute); text-align: right; font-size: 14px; }
.cx-call-row > summary > .cx-chevron::before { content: "▸"; }
.cx-call-row[open] > summary { background: var(--cx-cream-2); }
.cx-call-row[open] > summary > .cx-chevron::before { content: "▾"; color: var(--cx-gold); }
.cx-call-row > .cx-transcript {
  padding: 16px 22px; background: var(--cx-cream); font-size: 13px; line-height: 1.55;
  border-top: 1px solid var(--cx-rule);
}
.cx-transcript-turns .cx-turn { padding: 4px 0; }
.cx-transcript-turns .cx-speaker {
  font-weight: 600; color: var(--cx-navy);
  font-family: Geist Mono, monospace; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.05em;
  margin-right: 6px; min-width: 56px; display: inline-block;
}
.cx-transcript-turns .cx-speaker-agent  { color: var(--cx-navy); }
.cx-transcript-turns .cx-speaker-caller { color: var(--cx-gold); }
.cx-transcript-empty { color: var(--cx-mute); font-style: italic; margin: 0; }
.cx-transcript-raw {
  white-space: pre-wrap; word-break: break-word;
  font-family: Geist Mono, monospace; font-size: 11px;
  color: var(--cx-navy); margin: 0;
}
</style>
"""


# CSS scoped to the cohort collapsibles on the main report page. Outer <details>
# acts as a section banner; inner <details> are the per-call rows (re-using
# _CALL_ROWS_CSS classes for consistency).
_COHORT_CSS = """
<style>
.cx-cohort { border: 1px solid var(--cx-rule); border-radius: 6px;
  background: white; margin-top: 18px; }
.cx-cohort > summary {
  cursor: pointer; list-style: none; user-select: none;
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px;
  font-family: Geist Mono, monospace; font-size: 12px;
  text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--cx-navy); background: var(--cx-cream);
  border-radius: 6px;
}
.cx-cohort[open] > summary { border-bottom: 1px solid var(--cx-rule);
  border-radius: 6px 6px 0 0; background: var(--cx-cream-2); }
.cx-cohort > summary::-webkit-details-marker { display: none; }
.cx-cohort > summary::before {
  content: "▸"; color: var(--cx-mute); margin-right: 10px;
}
.cx-cohort[open] > summary::before { content: "▾"; color: var(--cx-gold); }
.cx-cohort-title { display: flex; gap: 10px; align-items: baseline; }
.cx-cohort-count {
  background: var(--cx-navy); color: var(--cx-cream);
  padding: 2px 8px; border-radius: 10px; font-size: 11px; letter-spacing: 0;
}
.cx-cohort-body { padding: 0; }
.cx-cohort-footer {
  display: flex; justify-content: flex-end; padding: 12px 18px;
  border-top: 1px solid var(--cx-rule);
}
.cx-see-full {
  display: inline-block; background: var(--cx-navy); color: var(--cx-cream);
  font-family: Geist Mono, monospace; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.08em;
  padding: 8px 14px; border-radius: 4px; text-decoration: none;
  border: 1px solid var(--cx-navy-3);
}
.cx-see-full:hover { background: var(--cx-navy-3); }
.cx-cohort-empty { padding: 14px 18px; color: var(--cx-mute);
  font-style: italic; font-size: 13px; }
</style>
"""


def _render_outcome_row(r: dict, transcript) -> str:
    """One row for the No-Conversation / QLNC layouts (6 columns).

    Column order: Description (LLM reasoning), Timestamp, Phone number,
    Connect duration, Duration, UTM medium, then chevron. Shared between the
    inline cohort previews and the standalone full pages.
    """
    transcript_html = _format_transcript(transcript)
    return (
        '<details class="cx-call-row">'
        '  <summary class="cx-row-6">'
        f'    <span>{escape(r["reasoning"] or "—")}</span>'
        f'    <span class="mono">{escape(_fmt_dt(r["start_time_local"]))}</span>'
        f'    <span class="mono">{escape(_fmt_phone(r["calling_phone_number"]))}</span>'
        f'    <span class="num">{_fmt_duration(r["connect_duration"])}</span>'
        f'    <span class="num">{_fmt_duration(r["duration"])}</span>'
        f'    <span>{escape(r["utm_medium"] or "—")}</span>'
        '    <span class="cx-chevron"></span>'
        '  </summary>'
        f'  <div class="cx-transcript">{transcript_html}</div>'
        '</details>'
    )


def _outcome_head_html() -> str:
    """Column header for outcome rows (No-Conv / QLNC layouts).

    Trailing empty span keeps the grid column count aligned with the body rows
    (which carry the chevron in that slot).
    """
    head_cells = ["Description", "Timestamp", "Phone number", "Connect", "Duration", "UTM medium"]
    return (
        '<div class="cx-call-row-head cx-row-6">'
        + "".join(f"<span>{escape(h)}</span>" for h in head_cells)
        + '<span></span>'
        + "</div>"
    )


def _render_invoice_row(r: dict, transcript_html: str, band: int) -> str:
    """One row for the Attributed Invoices layout (9 columns + chevron).

    Column order: Patient, Patient ID, Total, Invoice date, Invoice #, First
    call, First appt, UTM source, UTM medium, then chevron. ``transcript_html``
    is pre-formatted (because a single first-call transcript is shared across
    multiple invoice rows for the same patient).
    """
    bg_class = "cx-band-1" if band else "cx-band-0"
    name = " ".join(x for x in (r["given_name"], r["surname"]) if x) or "—"
    return (
        f'<details class="cx-call-row {bg_class}">'
        '  <summary class="cx-row-9">'
        f'    <span>{escape(name)}</span>'
        f'    <span class="mono">{escape(str(r["client_id"] or ""))}</span>'
        f'    <span class="num">{_fmt_money(r["order_total"])}</span>'
        f'    <span class="mono">{escape(str(r["invoice_date"] or ""))}</span>'
        f'    <span class="mono">{escape(str(r["invoice_number"] or r["invoice_order_id"] or ""))}</span>'
        f'    <span class="mono">{escape(_fmt_dt(r["first_call_ts"]))}</span>'
        f'    <span class="mono">{escape(_fmt_dt(r["appt_start_time"]))}</span>'
        f'    <span>{escape(r["utm_source"] or r["marketing_channel"] or "—")}</span>'
        f'    <span>{escape(r["utm_medium"] or "—")}</span>'
        '    <span class="cx-chevron"></span>'
        '  </summary>'
        f'  <div class="cx-transcript">{transcript_html}</div>'
        '</details>'
    )


# Per-band stripe + footer total CSS specific to the attributed-invoices layout.
_INVOICE_EXTRA_CSS = """
<style>
.cx-call-row.cx-band-0 > summary { background: white; }
.cx-call-row.cx-band-1 > summary { background: var(--cx-cream); }
.cx-call-row-total {
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 14px; background: var(--cx-navy); color: var(--cx-cream);
  font-family: Geist Mono, monospace; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.08em;
  border-top: 1px solid var(--cx-rule);
}
.cx-call-row-total .num { font-size: 14px; font-weight: 600; letter-spacing: 0; }
</style>
"""


def _invoice_head_html() -> str:
    head_cells = [
        "Patient", "Patient ID", "Total", "Invoice date", "Invoice #",
        "First call", "First appt", "UTM source", "UTM medium",
    ]
    return (
        '<div class="cx-call-row-head cx-row-9">'
        + "".join(f"<span>{escape(h)}</span>" for h in head_cells)
        + '<span></span>'  # placeholder for the chevron column on body rows
        + "</div>"
    )


def generate_spam_calls_report(
    clinic_id: str,
    clinic_name: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
) -> str:
    """Spam-call line items — drill-down from the Engagement section.

    PHI-light (just the caller's phone number) but still admin-gated upstream.
    Caller is responsible for serving with ``Content-Type: text/html``.
    """
    snapshot = q.blueprint_snapshot_date(clinic_id)
    today    = _dt.date.today().isoformat()

    rows_data = q.spam_calls_detail(clinic_id, invoca_campaign_ids, days=days)

    back_btn = (
        f'<a href="/intelligence/{escape(clinic_id)}" target="_top" '
        f'   style="color:var(--cx-gold);text-decoration:none;font-family:Geist Mono,monospace;'
        f'          font-size:11px;text-transform:uppercase;letter-spacing:0.08em;">'
        f'  ← Back to intelligence report'
        f'</a>'
    )

    header = (
        f'<header class="report-header">'
        f'  <div class="eyebrow">CORTEX · spam call line items</div>'
        f'  <h1>{escape(clinic_name)}</h1>'
        f'  <div class="meta">'
        + (
            f'    Blueprint snapshot: <b style="color:var(--cx-cream)">{escape(str(snapshot))}</b> &nbsp;·&nbsp;'
            if snapshot else ''
        )
        + f'    Generated {today} &nbsp;·&nbsp; Window: {days}d'
          f'  </div>'
          f'  <div style="margin-top:14px;">{back_btn}</div>'
          f'</header>'
    )

    if not rows_data:
        body = (
            header
            + '<section>'
            + '<h2><span class="num">--</span>No spam calls in window</h2>'
            + '<p class="lede">No inbound calls in the last '
            + f'<b>{days}</b> days were classified as spam by the LLM — or the clinic has no Invoca campaigns linked.</p>'
            + '</section>'
        )
        title = f"{clinic_name} — Spam calls · CORTEX"
        return _HEAD.format(title=escape(title)) + body + _FOOT

    # Fetch transcripts for every flagged call in one pass. Missing entries
    # (autodialer hangups without audio) are simply absent from the dict.
    from intelligence_report.transcripts import get_transcripts
    try:
        transcripts = get_transcripts([
            r["complete_call_id"] for r in rows_data if r.get("complete_call_id")
        ])
    except Exception as e:  # noqa: BLE001
        log.warning("transcripts unavailable for clinic_id=%s: %s", clinic_id, e)
        transcripts = {}

    head_cells = ["Call time", "Calling number", "Duration", "Connect", "Reason"]
    head_html = (
        '<div class="cx-call-row-head">'
        + "".join(f"<span>{escape(h)}</span>" for h in head_cells)
        + '<span></span>'  # chevron column placeholder
        + "</div>"
    )
    rows_html: list[str] = []
    for r in rows_data:
        ccid = r.get("complete_call_id") or ""
        transcript_html = _format_transcript(transcripts.get(ccid))
        rows_html.append(
            '<details class="cx-call-row">'
            '  <summary>'
            f'    <span class="mono">{escape(_fmt_dt(r["start_time_local"]))}</span>'
            f'    <span class="mono">{escape(_fmt_phone(r["calling_phone_number"]))}</span>'
            f'    <span class="num">{_fmt_duration(r["duration"])}</span>'
            f'    <span class="num">{_fmt_duration(r["connect_duration"])}</span>'
            f'    <span>{escape(r["spam_reason"])}</span>'
            '    <span class="cx-chevron"></span>'
            '  </summary>'
            f'  <div class="cx-transcript">{transcript_html}</div>'
            '</details>'
        )
    rows_block = (
        _CALL_ROWS_CSS
        + '<div class="cx-call-rows">'
        + head_html
        + "".join(rows_html)
        + '</div>'
    )

    section_html = _section(
        1, "Spam call detail",
        "One row per inbound call the LLM classified as spam — same filter applied "
        "upstream in §03's funnel. Click a row to expand the call transcript "
        "(when available). Sorted newest first.",
        rows_block,
    )

    body = (
        header
        + section_html
        + f'<footer>Built by CORTEX · {today} · clinic_id <code>{escape(clinic_id)}</code></footer>'
    )
    title = f"{clinic_name} — Spam calls · CORTEX"
    return _HEAD.format(title=escape(title)) + body + _FOOT


def _generate_outcome_detail_report(
    *,
    clinic_id: str,
    clinic_name: str,
    invoca_campaign_ids: list[str],
    days: int,
    title_label: str,
    eyebrow: str,
    lede: str,
    fetch: callable,
) -> str:
    """Shared renderer for the two Stage-2 leak detail pages (No Conversation,
    Qualified Lead — No Conversion). Same layout as the spam-calls page —
    per-call rows with timestamp, phone, duration, UTM, and Claude's
    reasoning. PHI-light (just the caller phone) but still admin-gated."""
    snapshot = q.blueprint_snapshot_date(clinic_id)
    today    = _dt.date.today().isoformat()

    rows_data = fetch(clinic_id, invoca_campaign_ids, days=days)

    back_btn = (
        f'<a href="/intelligence/{escape(clinic_id)}" target="_top" '
        f'   style="color:var(--cx-gold);text-decoration:none;font-family:Geist Mono,monospace;'
        f'          font-size:11px;text-transform:uppercase;letter-spacing:0.08em;">'
        f'  ← Back to intelligence report'
        f'</a>'
    )

    header = (
        f'<header class="report-header">'
        f'  <div class="eyebrow">CORTEX · {escape(eyebrow)}</div>'
        f'  <h1>{escape(clinic_name)}</h1>'
        f'  <div class="meta">'
        + (
            f'    Blueprint snapshot: <b style="color:var(--cx-cream)">{escape(str(snapshot))}</b> &nbsp;·&nbsp;'
            if snapshot else ''
        )
        + f'    Generated {today} &nbsp;·&nbsp; Window: {days}d'
          f'  </div>'
          f'  <div style="margin-top:14px;">{back_btn}</div>'
          f'</header>'
    )

    if not rows_data:
        body = (
            header
            + '<section>'
            + f'<h2><span class="num">--</span>No {escape(title_label)} calls in window</h2>'
            + '<p class="lede">'
            + f'No calls in the last <b>{days}</b> days fell into the <code>{escape(title_label)}</code> bucket, '
            + 'or the clinic\'s transcripts have not yet been scored.'
            + '</p></section>'
        )
        title = f"{clinic_name} — {title_label} · CORTEX"
        return _HEAD.format(title=escape(title)) + body + _FOOT

    # Fetch transcripts for every flagged call in one pass.
    from intelligence_report.transcripts import get_transcripts
    try:
        transcripts = get_transcripts([
            r["complete_call_id"] for r in rows_data if r.get("complete_call_id")
        ])
    except Exception as e:  # noqa: BLE001
        log.warning("transcripts unavailable for clinic_id=%s: %s", clinic_id, e)
        transcripts = {}

    rows_html = [
        _render_outcome_row(r, transcripts.get(r.get("complete_call_id") or ""))
        for r in rows_data
    ]
    rows_block = (
        _CALL_ROWS_CSS
        + '<div class="cx-call-rows">'
        + _outcome_head_html()
        + "".join(rows_html)
        + '</div>'
    )

    section_html = _section(
        1, f"{title_label} detail",
        lede + " Click a row to expand the call transcript.",
        rows_block,
    )

    body = (
        header
        + section_html
        + f'<footer>Built by CORTEX · {today} · clinic_id <code>{escape(clinic_id)}</code></footer>'
    )
    title = f"{clinic_name} — {title_label} · CORTEX"
    return _HEAD.format(title=escape(title)) + body + _FOOT


def generate_no_conversation_report(
    clinic_id: str,
    clinic_name: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
) -> str:
    """Detail page for Stage-2 "No Conversation" — calls that ended without
    any meaningful dialogue. Funnel terminates here per spec."""
    return _generate_outcome_detail_report(
        clinic_id=clinic_id,
        clinic_name=clinic_name,
        invoca_campaign_ids=invoca_campaign_ids,
        days=days,
        title_label="No Conversation",
        eyebrow="no-conversation line items",
        lede=(
            "Calls classified by callscoring as <code>no_conversation</code> — "
            "voicemails, hangups, silent autodials, etc. The funnel ends here."
        ),
        fetch=q.no_conversation_detail,
    )


def generate_qualified_no_conv_report(
    clinic_id: str,
    clinic_name: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
) -> str:
    """Detail page for Stage-2 "Qualified Lead — No Conversion" — calls where
    a real lead engaged but didn't book. The main conversion-leak bucket."""
    return _generate_outcome_detail_report(
        clinic_id=clinic_id,
        clinic_name=clinic_name,
        invoca_campaign_ids=invoca_campaign_ids,
        days=days,
        title_label="Qualified Lead — No Conversion",
        eyebrow="qualified-lead-no-conversion line items",
        lede=(
            "Calls where the caller was a bookable prospect and engaged in conversation, "
            "but no new appointment was booked. The clinic's biggest conversion-leak surface."
        ),
        fetch=q.qualified_lead_no_conv_detail,
    )
