"""
Export intelligence + line-item reports as PDFs for every active clinic.

Walks every instance and every non-deleted clinic in Cloud SQL, renders the
report HTMLs using the existing ``intelligence_report.report`` generators
(no HTTP round-trip — uses the in-process functions directly), and converts
each HTML to PDF with headless Chromium via Playwright.

Output layout:
    resources/reports/
      <instance-slug>/
        <clinic-slug>/
          intelligence.pdf
          spam_calls.pdf
          no_conversation.pdf
          qualified_no_conv.pdf
          attributed_invoices.pdf

Setup:
    source venv/bin/activate
    pip install playwright
    python -m playwright install chromium

Usage:
    cd cortex-hypervisor
    PYTHONPATH=. python scripts/export_reports.py
    PYTHONPATH=. python scripts/export_reports.py --instance "Alto Hearing"
    PYTHONPATH=. python scripts/export_reports.py --clinic-id <uuid>
    PYTHONPATH=. python scripts/export_reports.py --days 365 --line-item-days 90
    PYTHONPATH=. python scripts/export_reports.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import tempfile
from pathlib import Path

from sqlalchemy import select

from api.core.db import get_session
from api.core.orm import Clinic, GoogleAdsCampaign, Instance, InvocaCampaign
from intelligence_report.report import (
    generate_attributed_invoices_report,
    generate_no_conversation_report,
    generate_qualified_no_conv_report,
    generate_report_with_campaigns,
    generate_spam_calls_report,
)

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_ROOT = REPO_ROOT / "resources" / "reports"

# Each entry: (filename, render_fn, args/kwargs builder).
# Builder receives (clinic_id, clinic_name, invoca_ids, ga_ids, days,
# line_item_days) and returns the dict of kwargs to pass to the render function.
REPORTS = [
    (
        "intelligence",
        generate_report_with_campaigns,
        lambda cid, cname, iv, ga, days, li_days: dict(
            clinic_id=cid,
            clinic_name=cname,
            invoca_campaign_ids=iv,
            google_ads_campaign_ids=ga,
            days=days,
            use_cache=False,
        ),
    ),
    (
        "spam_calls",
        generate_spam_calls_report,
        lambda cid, cname, iv, ga, days, li_days: dict(
            clinic_id=cid,
            clinic_name=cname,
            invoca_campaign_ids=iv,
            days=li_days,
        ),
    ),
    (
        "no_conversation",
        generate_no_conversation_report,
        lambda cid, cname, iv, ga, days, li_days: dict(
            clinic_id=cid,
            clinic_name=cname,
            invoca_campaign_ids=iv,
            days=li_days,
        ),
    ),
    (
        "qualified_no_conv",
        generate_qualified_no_conv_report,
        lambda cid, cname, iv, ga, days, li_days: dict(
            clinic_id=cid,
            clinic_name=cname,
            invoca_campaign_ids=iv,
            days=li_days,
        ),
    ),
    (
        "attributed_invoices",
        generate_attributed_invoices_report,
        lambda cid, cname, iv, ga, days, li_days: dict(
            clinic_id=cid,
            clinic_name=cname,
            invoca_campaign_ids=iv,
            days=days,
        ),
    ),
]


def slugify(name: str) -> str:
    """Filesystem-safe lowercase slug. ``"Alto Hearing - North"`` → ``alto-hearing-north``."""
    s = (name or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)        # strip punctuation
    s = re.sub(r"[\s_-]+", "-", s)         # collapse whitespace + underscores
    s = s.strip("-")
    return s or "unnamed"


def fetch_targets(
    db,
    instance_filter: str | None,
    clinic_id_filter: str | None,
) -> list[tuple[Instance, Clinic]]:
    """Resolve the (instance, clinic) pairs to export.

    Returns non-deleted clinics. ``instance_filter`` matches case-insensitive
    substring against ``instance_name``. ``clinic_id_filter`` is exact.
    """
    q = (
        select(Instance, Clinic)
        .join(Clinic, Clinic.instance_id == Instance.instance_id)
        .where(Clinic.deleted_at.is_(None))
        .order_by(Instance.instance_name.asc(), Clinic.clinic_name.asc())
    )
    if clinic_id_filter:
        q = q.where(Clinic.clinic_id == clinic_id_filter)
    rows = db.execute(q).all()
    if instance_filter:
        needle = instance_filter.lower()
        rows = [(i, c) for i, c in rows if needle in (i.instance_name or "").lower()]
    return rows


def campaign_ids_for(db, clinic_id: str) -> tuple[list[str], list[str]]:
    """Return ``(invoca_ids, google_ads_ids)`` for a clinic — active only."""
    invoca = [
        str(x) for x in db.scalars(
            select(InvocaCampaign.invoca_campaign_id).where(
                InvocaCampaign.clinic_id == clinic_id,
                InvocaCampaign.active.is_(True),
            )
        )
    ]
    google_ads = [
        str(x) for x in db.scalars(
            select(GoogleAdsCampaign.google_ads_campaign_id).where(
                GoogleAdsCampaign.clinic_id == clinic_id,
                GoogleAdsCampaign.active.is_(True),
            )
        )
    ]
    return invoca, google_ads


def render_pdf(html: str, pdf_path: Path, browser, *, wait_ms: int = 2000) -> None:
    """Render an HTML string to a single PDF file.

    Loads the HTML from a temporary file so all relative URLs (Plotly CDN, etc.)
    behave consistently. Waits for ``networkidle`` plus a configurable delay so
    Plotly figures finish rendering before the PDF snapshot is taken.
    """
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", encoding="utf-8", delete=False
    ) as fp:
        fp.write(html)
        html_path = Path(fp.name)
    try:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
        page.wait_for_timeout(wait_ms)
        page.pdf(
            path=str(pdf_path),
            format="A3",
            landscape=True,
            print_background=True,
            margin={"top": "10mm", "bottom": "10mm",
                    "left": "10mm", "right": "10mm"},
        )
        page.close()
    finally:
        html_path.unlink(missing_ok=True)


def export_clinic(
    db,
    instance: Instance,
    clinic: Clinic,
    *,
    days: int,
    line_item_days: int,
    output_root: Path,
    browser,
    dry_run: bool,
) -> tuple[int, int]:
    """Render all 5 reports for one clinic. Returns ``(ok_count, fail_count)``.

    Per-report exceptions are isolated — one failing report doesn't stop the
    others.
    """
    instance_slug = slugify(instance.instance_name or instance.instance_id)
    clinic_slug = slugify(clinic.clinic_name or clinic.clinic_id)
    clinic_dir = output_root / instance_slug / clinic_slug

    invoca_ids, google_ads_ids = campaign_ids_for(db, clinic.clinic_id)
    log.info(
        "→ %s / %s (invoca=%d, google_ads=%d)",
        instance.instance_name, clinic.clinic_name,
        len(invoca_ids), len(google_ads_ids),
    )

    if dry_run:
        for filename, _, _ in REPORTS:
            log.info("   would write %s", clinic_dir / f"{filename}.pdf")
        return (len(REPORTS), 0)

    ok = 0
    fail = 0
    for filename, render_fn, kwargs_builder in REPORTS:
        out_path = clinic_dir / f"{filename}.pdf"
        try:
            kwargs = kwargs_builder(
                clinic.clinic_id, clinic.clinic_name,
                invoca_ids, google_ads_ids,
                days, line_item_days,
            )
            html = render_fn(**kwargs)
            render_pdf(html, out_path, browser)
            log.info("   ✓ %s.pdf (%d KB)", filename, out_path.stat().st_size // 1024)
            ok += 1
        except Exception as e:  # noqa: BLE001
            log.exception("   ✗ %s.pdf failed: %s", filename, e)
            fail += 1
    return ok, fail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--instance", default=None,
                        help="Filter by instance_name (case-insensitive substring).")
    parser.add_argument("--clinic-id", default=None,
                        help="Render a single clinic by clinic_id.")
    parser.add_argument("--days", type=int, default=365,
                        help="Window for the main intelligence + attributed-invoices reports.")
    parser.add_argument("--line-item-days", type=int, default=90,
                        help="Window for spam-calls / no-conversation / qualified-no-conv pages.")
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT,
                        help=f"Output root (default: {OUTPUT_ROOT}).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print which PDFs would be produced without rendering.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    with next(get_session()) as db:
        targets = fetch_targets(db, args.instance, args.clinic_id)
        if not targets:
            log.warning("No clinics matched filters.")
            return 0
        log.info("Resolved %d clinic(s).", len(targets))

        # Single browser shared across every render to amortise startup.
        if args.dry_run:
            total_ok = total_fail = 0
            for instance, clinic in targets:
                ok, fail = export_clinic(
                    db, instance, clinic,
                    days=args.days,
                    line_item_days=args.line_item_days,
                    output_root=args.output,
                    browser=None,
                    dry_run=True,
                )
                total_ok += ok
                total_fail += fail
            log.info("Dry-run done. Would produce %d PDFs.", total_ok)
            return 0

        args.output.mkdir(parents=True, exist_ok=True)

        # Playwright import here so --help and --dry-run work without it.
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error(
                "Playwright not installed. In the hypervisor venv:\n"
                "  pip install playwright\n"
                "  python -m playwright install chromium"
            )
            return 1

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                total_ok = total_fail = 0
                for instance, clinic in targets:
                    ok, fail = export_clinic(
                        db, instance, clinic,
                        days=args.days,
                        line_item_days=args.line_item_days,
                        output_root=args.output,
                        browser=browser,
                        dry_run=False,
                    )
                    total_ok += ok
                    total_fail += fail
            finally:
                browser.close()

    log.info(
        "─" * 60 + "\n"
        "Done. %d PDFs written, %d failed. Output: %s",
        total_ok, total_fail, args.output,
    )
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
