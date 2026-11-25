"""
OpenAlex Ongoing Incremental Monitor
====================================
Monitors OpenAlex Free Plan for newly published or retroactively indexed
academic papers matching the topic configured in `domain_config.json`, without
requiring paid Member+ daily-sync filters (`from_created_date` / `from_updated_date`).

Two Free-Plan compatible modes:
  1. `publication` (default, 1 API call = $0.001):
     Uses `from_publication_date:{since_date}` + `sort=publication_date:desc`
     with a 30-day safety overlap window (`last_success_date - 30d`, or 60d
     on first run) to catch newly published papers even with short indexing lag.
  2. `full-diff` (retroactive catch-up, ~4 API calls = $0.004):
     Scans the entire `OPENALEX_BASE_FILTER` Stage-1 pool and diffs against
     `zotero_cache.json` + `rejected_cache.json` to catch older-year papers
     that OpenAlex indexes retroactively.

Maintains its own lightweight watermark in `incremental_state.json` without
touching `search_state.json`, and reuses all filtering, LLM classification,
passing_mention gatekeeping, and Zotero ingestion logic from `openalextest.py`.

Usage:
  python openalex_incremental.py                         # Auto watermark (last_success_date - 30d, or 60d first run)
  python openalex_incremental.py --dry-run               # Preview new candidates without LLM/Zotero/state writes
  python openalex_incremental.py --days 30               # Look back N publication days from today
  python openalex_incremental.py --since 2026-10-01      # Explicit publication start date (YYYY-MM-DD)
  python openalex_incremental.py --mode full-diff        # Retroactive full Stage-1 diff on Free Plan
"""

import argparse
import datetime
import json
import os
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple

import openalextest as core

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
INCREMENTAL_STATE_FILE = "incremental_state.json"
DEFAULT_FIRST_RUN_DAYS = 60
DEFAULT_OVERLAP_DAYS = 30


def load_incremental_state(filepath: str = INCREMENTAL_STATE_FILE) -> Dict[str, Any]:
    """Load incremental polling watermark state from JSON."""
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_incremental_state(
    state: Dict[str, Any], filepath: str = INCREMENTAL_STATE_FILE
) -> None:
    """Persist incremental polling watermark atomically."""
    core.atomic_json_write(filepath, state)


def compute_since_date(
    state: Dict[str, Any],
    since_arg: Optional[str] = None,
    days_arg: Optional[int] = None,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
    today: Optional[datetime.date] = None,
) -> Tuple[str, str]:
    """
    Compute the starting publication date (YYYY-MM-DD) and its provenance note.
    Priority: --since > --days > incremental_state.json (minus safety overlap) > 60-day default on first run.
    """
    ref_today = today or datetime.date.today()

    if since_arg:
        parsed = datetime.date.fromisoformat(since_arg.strip())
        return parsed.isoformat(), f"CLI --since={parsed.isoformat()}"

    if days_arg is not None:
        if days_arg <= 0:
            raise ValueError("--days must be a positive integer")
        target = ref_today - datetime.timedelta(days=days_arg)
        return target.isoformat(), f"CLI --days={days_arg} lookback"

    last_date_str = state.get("last_success_date")
    if isinstance(last_date_str, str) and last_date_str.strip():
        try:
            last_dt = datetime.date.fromisoformat(last_date_str.strip())
            safe_overlap = max(0, overlap_days)
            target = last_dt - datetime.timedelta(days=safe_overlap)
            return (
                target.isoformat(),
                f"last watermark {last_dt.isoformat()} minus {safe_overlap}d safety overlap",
            )
        except ValueError:
            pass

    target = ref_today - datetime.timedelta(days=DEFAULT_FIRST_RUN_DAYS)
    return target.isoformat(), f"first-run default {DEFAULT_FIRST_RUN_DAYS}d lookback"


def build_incremental_filter(since_date: str, mode: str = "publication") -> str:
    """Build Free-Plan-compatible OpenAlex filter expression."""
    if mode == "full-diff":
        return core.OPENALEX_BASE_FILTER
    return f"{core.OPENALEX_BASE_FILTER},from_publication_date:{since_date}"


def fetch_incremental_papers(
    since_date: str,
    existing_titles: Set[str],
    existing_dois: Set[str],
    mode: str = "publication",
    limit: Optional[int] = None,
    per_page: int = 200,
    max_pages: int = 25,
    max_retries: int = 3,
    max_rate_retries: int = 5,
) -> Tuple[List[dict], Dict[str, Any]]:
    """
    Fetch newly published or retroactively indexed candidate papers within OpenAlex Free Plan limits.
    Returns (valid_papers, stats), where stats['complete'] indicates whether the scan completed untruncated.
    """
    filter_expr = build_incremental_filter(since_date, mode=mode)
    sort_expr = "publication_date:desc"
    valid_papers: List[dict] = []
    stats: Dict[str, Any] = {
        "since_date": since_date,
        "mode": mode,
        "total_hits": 0,
        "pages_scanned": 0,
        "raw_scanned": 0,
        "selected": 0,
        "complete": True,
    }
    page = 1

    if mode == "full-diff":
        mode_desc = "full-diff retroactive Stage-1 pool scan"
    else:
        mode_desc = f"from_publication_date >= {since_date}"

    print(
        f"📡 [OpenAlex Incremental] Mode: {mode_desc} (per_page={per_page}"
        + (f", limit={limit}" if limit else "")
        + ")..."
    )

    while page <= max_pages and (limit is None or len(valid_papers) < limit):
        params = {
            "filter": filter_expr,
            "sort": sort_expr,
            "per_page": per_page,
            "page": page,
        }

        payload: Optional[dict] = None
        net_attempts = 0
        rate_attempts = 0
        page_error: Optional[str] = None

        while True:
            try:
                time.sleep(0.6)
                response = core.openalex_session.get(
                    OPENALEX_WORKS_URL, params=params, timeout=25
                )

                if response.status_code == 200:
                    payload = response.json()
                    break

                elif response.status_code == 429:
                    retry_after_raw = str(response.headers.get("Retry-After", "")).strip()
                    retry_after_sec = (
                        int(retry_after_raw) if retry_after_raw.isdigit() else None
                    )
                    rem_usd_raw = str(
                        response.headers.get("X-RateLimit-Remaining-USD", "")
                    ).strip()
                    rem_usd = core._parse_float_header(
                        response.headers, "X-RateLimit-Remaining-USD"
                    )
                    body_lower = (response.text or "").lower()

                    is_daily_exhausted = (
                        "insufficient budget" in body_lower
                        or (rem_usd is not None and rem_usd < 0.005)
                        or (retry_after_sec is not None and retry_after_sec > 120)
                    )
                    if is_daily_exhausted:
                        raise core.OpenAlexQuotaExceededError(
                            f"OpenAlex daily quota exhausted (Remaining=${rem_usd_raw or '?'}, Retry-After={retry_after_raw or '?'}s)",
                            partial_papers=valid_papers,
                        )

                    rate_attempts += 1
                    if rate_attempts <= max_rate_retries:
                        base_wait = (
                            (retry_after_sec + 1)
                            if retry_after_sec is not None
                            else (rate_attempts * 4)
                        )
                        wait_sec = min(max(base_wait, rate_attempts * 3), 60)
                        print(
                            f"⏳ [OpenAlex 429 Pacing] Retry-After={retry_after_raw or '?'}s, "
                            f"Remaining=${rem_usd_raw or '?'} — waiting {wait_sec}s before retry "
                            f"({rate_attempts}/{max_rate_retries})"
                        )
                        time.sleep(wait_sec)
                        continue
                    raise core.OpenAlexQuotaExceededError(
                        f"OpenAlex persistent 429 rate limit after {max_rate_retries} retries (Remaining=${rem_usd_raw or '?'})",
                        partial_papers=valid_papers,
                    )

                else:
                    msg = f"HTTP {response.status_code}"
                    print(
                        f"⚠️ [OpenAlex {msg}] Incremental page {page} request failed: "
                        f"{(response.text or '')[:160]}"
                    )
                    page_error = msg
                    break

            except core.OpenAlexQuotaExceededError:
                raise
            except Exception as e:
                net_attempts += 1
                print(
                    f"⚠️ [OpenAlex Network Error] Incremental page {page} "
                    f"attempt {net_attempts}/{max_retries} failed: {e}"
                )
                if net_attempts < max_retries:
                    time.sleep(2 * net_attempts)
                else:
                    page_error = f"Network error: {e}"
                    break

        if page_error is not None:
            stats["complete"] = False
            if valid_papers:
                print(
                    f"⚠️ Page {page} request failed ({page_error}); "
                    f"preserving {len(valid_papers)} collected candidates (watermark will not advance)."
                )
                break
            raise core.OpenAlexRequestError(page_error)

        results = (payload or {}).get("results") or []
        total_hits = ((payload or {}).get("meta") or {}).get("count")
        if total_hits is not None:
            stats["total_hits"] = total_hits

        if not results:
            if page == 1:
                empty_hint = (
                    "No matching works in OpenAlex Stage-1 pool."
                    if mode == "full-diff"
                    else f"No new matching works in OpenAlex since {since_date}."
                )
                print(f"📭 {empty_hint}")
            break

        stats["pages_scanned"] = page
        stats["raw_scanned"] += len(results)

        for work in results:
            try:
                parsed = core._parse_openalex_work(work, existing_titles, existing_dois)
            except Exception as parse_err:
                err_id = work.get("id", "unknown") if isinstance(work, dict) else "non-dict"
                print(f"⚠️ Skipping malformed OpenAlex work ({err_id}): {parse_err}")
                continue

            if not parsed:
                continue

            paper_data, norm_title, doi = parsed
            valid_papers.append(paper_data)
            existing_titles.add(norm_title)
            if doi:
                existing_dois.add(doi)

            if limit is not None and len(valid_papers) >= limit:
                stats["complete"] = False
                break

        hits_info = f"total_hits={total_hits}, " if total_hits is not None else ""
        print(
            f"📄 OpenAlex Incremental page {page} ({hits_info}page_items={len(results)}) | "
            f"Collected candidates: {len(valid_papers)}"
        )

        if (
            (limit is not None and len(valid_papers) >= limit)
            or len(results) < per_page
            or (total_hits is not None and page * per_page >= total_hits)
        ):
            break

        page += 1

    stats["selected"] = len(valid_papers)
    return valid_papers, stats


def print_dry_run_summary(papers: List[dict], stats: Dict[str, Any]) -> None:
    """Read-only preview mode: print incremental window hits and candidate samples."""
    type_counter = Counter(p.get("workType") or "unknown" for p in papers)
    scope_str = (
        "full-diff (Stage-1 pool diff)"
        if stats.get("mode") == "full-diff"
        else f"from_publication_date >= {stats['since_date']}"
    )
    print("\n" + "=" * 56)
    print("🧪 [DRY-RUN COMPLETE] LLM was not called; Zotero and watermark unchanged")
    print(
        f"📅 Mode: {scope_str} | "
        f"OpenAlex hits: {stats['total_hits']} | New deduplicated candidates: {len(papers)}"
    )
    print(f"📊 Work types: {dict(type_counter)}")
    if papers:
        print("📋 Sample candidates (first 10):")
        for i, p in enumerate(papers[:10], 1):
            print(
                f"   [{i:2d}] [{p.get('year') or '?'}] ({p.get('workType') or '?'}) "
                f"cite={p.get('citationCount', 0):3d} | {(p.get('title') or '')[:75]}"
            )
    print("=" * 56)


def classify_and_ingest(papers: List[dict], chunk_size: int = 50) -> Dict[str, int]:
    """Run LLM classification, passing-mention gatekeeping, and chunked Zotero push."""
    totals = {"core": 0, "case_study": 0, "rejected": 0, "pushed": 0}
    total_papers = len(papers)
    safe_chunk = max(1, min(chunk_size, 50))

    for start in range(0, total_papers, safe_chunk):
        chunk = papers[start : start + safe_chunk]
        items_to_push: List[dict] = []
        new_titles_added: List[str] = []
        new_dois_added: List[str] = []
        rejected_records: List[dict] = []

        for offset, paper in enumerate(chunk, 1):
            global_idx = start + offset
            title = paper.get("title") or ""
            abstract = paper.get("abstract") or ""
            venue = paper.get("venue") or ""
            work_type = paper.get("workType") or ""
            source_type = paper.get("sourceType") or ""
            citations = paper.get("citationCount", 0)
            doi = (paper.get("externalIds") or {}).get("DOI", "")

            print(f"--- Incremental Review [{global_idx}/{total_papers}] ---")
            print(f"📖 Title: {title}")
            print(f"📊 Citations: {citations} | OpenAlex type: {work_type or 'unknown'}")

            ai_result = core.classify_with_llm(
                title,
                abstract,
                venue=venue,
                work_type=work_type,
                source_type=source_type,
            )

            ac_rel = ai_result.get("relevance") or ai_result.get("ac_relevance", "case_study")
            doc_type = ai_result.get("doc_type", "research-article")
            print(f"🎯 Relevance: {ac_rel} | DocType: {doc_type}")

            if ac_rel == "passing_mention":
                print(
                    f"🚫 [Gatekeeper] Classified as passing_mention; skipping Zotero and recording in {core.REJECTED_CACHE_FILE}"
                )
                rejected_records.append(
                    {
                        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "title": title,
                        "doi": doi,
                        "reason": "passing_mention",
                        "summary": ai_result.get("one_sentence_summary", ""),
                    }
                )
                totals["rejected"] += 1
                print("-" * 40)
                time.sleep(0.3)
                continue

            if ac_rel == "core":
                totals["core"] += 1
            else:
                totals["case_study"] += 1

            print(f"📂 Assigned Collection Keys: {ai_result.get('collection_keys')}")
            print(f"🏷️ Tags: {ai_result.get('tags')}")
            print(f"📝 English Summary: {ai_result.get('one_sentence_summary')}")

            item = core.prepare_zotero_item(paper, ai_result)
            items_to_push.append(item)
            new_titles_added.append(title)
            if doi:
                new_dois_added.append(doi)

            print("-" * 40)
            time.sleep(0.3)

        if items_to_push:
            core.batch_push_to_zotero(items_to_push, chunk_size=50)
            totals["pushed"] += len(items_to_push)
            latest_version = None
            try:
                latest_version = core.zot.last_modified_version()
            except Exception:
                pass
            core.save_to_local_cache(
                new_titles_added, new_dois_added, cloud_version=latest_version
            )

        if rejected_records:
            core.save_rejected_cache(rejected_records)
            print(
                f"🛡️ Persisted {len(rejected_records)} rejected record(s) to {core.REJECTED_CACHE_FILE}."
            )

    print(
        f"\n📊 [Incremental Summary] Core={totals['core']} | "
        f"Case study={totals['case_study']} | "
        f"Pushed to Zotero={totals['pushed']} | "
        f"Rejected={totals['rejected']}"
    )
    return totals


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OpenAlex ongoing incremental monitor (Free Plan compatible: publication polling + full-diff catch-up)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read-only preview: inspect new candidates without calling the LLM, writing Zotero, or advancing the watermark",
    )
    parser.add_argument(
        "--mode",
        choices=("publication", "full-diff"),
        default="publication",
        help="Incremental mode: publication=newly published (1 call, default); full-diff=Stage-1 pool diff (~4 calls)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Look back N publication days from today (overrides saved watermark)",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Explicit publication start date YYYY-MM-DD (highest priority)",
    )
    parser.add_argument(
        "--overlap",
        type=int,
        default=DEFAULT_OVERLAP_DAYS,
        help=f"Safety overlap days subtracted from saved watermark (default: {DEFAULT_OVERLAP_DAYS}d to absorb indexing lag)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap number of candidates processed in this run (watermark will not advance if truncated)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=50,
        help="Push to Zotero and checkpoint caches after every N candidates (1–50, default: 50)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("❌ --limit must be a positive integer.")
    if not core.ROOT_COLLECTION_KEY:
        raise SystemExit("❌ ZOTERO_ROOT_COLLECTION_KEY is not configured in .env.")

    if core.OPENALEX_API_KEY:
        print("🔑 OpenAlex API Key loaded ($1.00/day dedicated quota mode)")
    else:
        print("⚠️ OPENALEX_API_KEY not configured; using shared anonymous IP quota mode")
    print(f"🤖 LLM Provider: {core.LLM_PROVIDER} | Model: {core.LLM_MODEL}")

    state = load_incremental_state()
    try:
        since_date, reason = compute_since_date(
            state=state,
            since_arg=args.since,
            days_arg=args.days,
            overlap_days=args.overlap,
        )
    except ValueError as e:
        raise SystemExit(f"❌ Invalid date argument: {e}")

    if args.mode == "full-diff":
        print("🧭 Strategy: full-diff (Free Plan Stage-1 retroactive diff)")
    else:
        print(f"🗓️ Incremental start date: {since_date} ({reason}) | Strategy: publication (from_publication_date)")

    existing_titles, existing_dois = core.get_existing_zotero_cache(
        core.ROOT_COLLECTION_KEY
    )
    rejected_titles, rejected_dois = core.load_rejected_cache()
    seen_titles = existing_titles | rejected_titles
    seen_dois = existing_dois | rejected_dois
    if rejected_titles or rejected_dois:
        print(
            f"🛡️ Loaded rejection cache {core.REJECTED_CACHE_FILE} "
            f"({len(rejected_titles)} rejected/excluded titles will be skipped)."
        )

    quota_aborted = False
    try:
        papers, stats = fetch_incremental_papers(
            since_date=since_date,
            existing_titles=seen_titles,
            existing_dois=seen_dois,
            mode=args.mode,
            limit=args.limit,
        )
    except core.OpenAlexQuotaExceededError as e:
        quota_aborted = True
        papers = e.partial_papers
        stats = {
            "since_date": since_date,
            "mode": args.mode,
            "total_hits": 0,
            "pages_scanned": 0,
            "raw_scanned": 0,
            "selected": len(papers),
            "complete": False,
        }
        print(f"\n🛑 [Circuit Breaker] {e}! Processing {len(papers)} collected candidate(s) without advancing watermark.")
    except core.OpenAlexRequestError as e:
        print(f"❌ OpenAlex incremental request failed: {e} (watermark unchanged).")
        return

    if args.dry_run:
        print_dry_run_summary(papers, stats)
        return

    totals = {"core": 0, "case_study": 0, "rejected": 0, "pushed": 0}
    if papers:
        print(f"\n✅ Found {len(papers)} new candidate(s); starting LLM classification and Zotero ingestion...\n")
        totals = classify_and_ingest(papers, chunk_size=args.chunk_size)
    else:
        empty_msg = (
            "No unharvested Stage-1 papers remain in OpenAlex."
            if args.mode == "full-diff"
            else f"No new unharvested {core.TOPIC_NAME} papers found since {since_date}."
        )
        print(f"✅ {empty_msg}")

    if stats.get("complete") and not quota_aborted:
        today_str = datetime.date.today().isoformat()
        new_state = {
            "last_success_date": today_str,
            "last_run_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": args.mode,
            "overlap_days": max(0, args.overlap),
            "last_since_date": since_date,
            "last_total_hits": stats.get("total_hits", 0),
            "last_candidates": len(papers),
            "last_ingested": totals["pushed"],
            "last_rejected": totals["rejected"],
        }
        save_incremental_state(new_state)
        print(f"📝 Saved incremental watermark ({today_str}) to {INCREMENTAL_STATE_FILE}.")
    else:
        print(f"⚠️ Incremental scan did not cover the full window (truncated or tripped circuit breaker); watermark in {INCREMENTAL_STATE_FILE} unchanged.")


if __name__ == "__main__":
    main()
