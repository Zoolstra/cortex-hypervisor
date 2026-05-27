"""
Per-clinic intelligence report.

Generates an HTML page summarising a clinic's Blueprint-backed practice data:
patient mix, appointment outcomes, revenue, top referral sources, and (when
the clinic has linked campaigns) inbound call funnel from Invoca + Google Ads.

The module is deliberately stdlib-only (no pandas / plotly) so it can live in
the hypervisor container without bloating the image. Charts are CSS/SVG
inline; tables are plain HTML. Future sessions can layer Plotly charts and
Claude-driven transcript analysis on top.

Entry point:
    from intelligence_report import generate_report
    html = generate_report(clinic_id, clinic_name)
"""
from intelligence_report.report import generate_report

__all__ = ["generate_report"]
