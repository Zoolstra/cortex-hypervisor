"""
GCS transcript fetcher for the intelligence-report line-item drill-downs.

Transcripts are produced by big-query-ingestion/transcript_analysis and stored
in ``gs://transcripts-json`` keyed by ``{complete_call_id}.json``. Skipped
calls (no audio / no transcript available from Invoca) get a sentinel
``{ccid}.skip`` blob instead — those return ``None`` here.

IAM: the hypervisor's runtime service account needs ``roles/storage.objectViewer``
on the ``transcripts-json`` bucket. Without it, ``get_transcripts`` will raise
on the first call.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from google.api_core import exceptions as gcp_exc
from google.cloud import storage

log = logging.getLogger(__name__)

TRANSCRIPTS_BUCKET = "transcripts-json"

# Parallelism for the GCS roundtrips. Each fetch is I/O-bound (network only),
# so threading is appropriate even under the GIL. 32 was chosen by eyeballing:
# enough to saturate a typical Cloud Run instance's outbound bandwidth without
# tripping GCS per-client connection limits.
_MAX_WORKERS = 32

# Cached storage client — lazy so import doesn't probe ADC. The
# ``google-cloud-storage`` client is thread-safe.
_client: storage.Client | None = None


def _get_client() -> storage.Client:
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def _fetch_one(bucket: storage.Bucket, ccid: str) -> tuple[str, Any | None]:
    """Fetch a single transcript blob. Returns ``(ccid, json | None)``.

    ``None`` for NotFound (expected: spam calls and short hangups usually have
    no transcript). Other exceptions are logged and treated as missing so a
    single bad blob doesn't crash the page render.
    """
    blob = bucket.blob(f"{ccid}.json")
    try:
        return ccid, json.loads(blob.download_as_text())
    except gcp_exc.NotFound:
        return ccid, None
    except Exception as e:  # noqa: BLE001
        log.warning("Could not load transcript %s from GCS: %s", ccid, e)
        return ccid, None


def get_transcripts(ccids: list[str]) -> dict[str, Any]:
    """Fetch transcripts for a list of complete_call_ids from GCS in parallel.

    Returns ``{ccid: transcript_json}`` for ccids that have a stored transcript.
    Missing entries (no blob, or a ``.skip`` sentinel) are simply absent from
    the returned dict — callers should treat them as "no transcript".

    Fetches blobs by name (``storage.objects.get``) rather than listing the
    bucket, so this works with viewer roles that don't include list. Runs the
    per-ccid lookups concurrently across a thread pool, since each fetch is
    network-bound: a line-item page with 100 ccids drops from ~10 s serial
    to <1 s with 32 workers.
    """
    unique_ccids = list({c for c in ccids if c})
    if not unique_ccids:
        return {}

    bucket = _get_client().bucket(TRANSCRIPTS_BUCKET)
    workers = min(_MAX_WORKERS, len(unique_ccids)) or 1

    out: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="transcripts") as pool:
        for ccid, transcript in pool.map(lambda c: _fetch_one(bucket, c), unique_ccids):
            if transcript is not None:
                out[ccid] = transcript
    return out
