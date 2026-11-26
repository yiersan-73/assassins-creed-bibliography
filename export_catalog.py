"""
Zotero Public Group Catalog Exporter (CC0 / CC BY 4.0 Compliant)
==================================================================
Exports curated bibliographic metadata, 4D taxonomy memberships, controlled tags,
and original English AI triage summaries from a public Zotero Group (or personal
library with API key) into static JSON and CSV datasets for GitHub, Zenodo,
Hugging Face, and the zero-dependency GitHub Pages web catalog.

Strict Copyright & Privacy Safeguards:
- NEVER exports publisher-copyrighted abstracts (`abstractNote`).
- NEVER exports Zotero creator/modifier usernames (`createdByUser`).
- Supports `--skip-if-unchanged` so scheduled GitHub Actions runs remain idempotent.

Usage:
  python export_catalog.py --group-id 6777746
  python export_catalog.py --group-id 6777746 --skip-if-unchanged
"""

import argparse
from collections import Counter, defaultdict
import csv
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

ZOTERO_API_BASE = "https://api.zotero.org"
PREFERRED_DIM_ORDER = ["Games", "Subjects", "Topics", "Languages"]
VALID_DOC_TYPES = {
    "research-article",
    "thesis",
    "book-chapter",
    "conference-paper",
    "book",
    "book-review",
    "other",
}
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
EXTRA_FIELD_RE = re.compile(
    r"^(Relevance|DocType|Citations|Retrieved|AI Summary)\s*:\s*(.*)$",
    re.IGNORECASE,
)


def atomic_write_text(filepath: Path, content: str) -> None:
    """Atomically write UTF-8 text to disk via a temporary file in the target directory."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=str(filepath.parent), delete=False, encoding="utf-8", newline=""
    ) as tf:
        tf.write(content)
        temp_name = tf.name
    os.replace(temp_name, filepath)


def normalize_ws(text: Any) -> str:
    """Collapse internal whitespace cleanly."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def clean_doi(raw_doi: Any) -> str:
    """Normalize DOI to bare lowercase identifier without resolver URL prefix."""
    s = normalize_ws(raw_doi)
    if not s:
        return ""
    s = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", s, flags=re.IGNORECASE).strip()
    return s.lower()


def extract_year(date_str: Any) -> Optional[int]:
    """Extract a 4-digit publication year from a Zotero date string."""
    s = normalize_ws(date_str)
    if not s:
        return None
    m = YEAR_RE.search(s)
    return int(m.group(1)) if m else None


def parse_extra_metadata(extra_raw: Any) -> Dict[str, Any]:
    """Parse Relevance, DocType, Citations, and AI Summary from Zotero Extra field."""
    result: Dict[str, Any] = {
        "relevance": "",
        "doc_type": "",
        "citations": None,
        "ai_summary": "",
    }
    if not extra_raw:
        return result

    ai_lines: List[str] = []
    in_ai_summary = False
    for raw_line in str(extra_raw).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = EXTRA_FIELD_RE.match(line)
        if m:
            key = m.group(1).lower()
            val = m.group(2).strip()
            in_ai_summary = False
            if key == "relevance":
                result["relevance"] = re.sub(r"[\s\-]+", "_", val.lower())
            elif key == "doctype":
                result["doc_type"] = re.sub(r"[\s_]+", "-", val.lower())
            elif key == "citations":
                digits = re.sub(r"[^\d]", "", val)
                if digits:
                    result["citations"] = int(digits)
            elif key == "ai summary":
                in_ai_summary = True
                if val:
                    ai_lines.append(val)
        elif in_ai_summary:
            ai_lines.append(line)

    if ai_lines:
        result["ai_summary"] = normalize_ws(" ".join(ai_lines))
    return result


def format_creators(creators: List[Dict[str, Any]], target_type: str) -> Tuple[List[Dict[str, str]], str]:
    """Extract structured creator records and readable semicolon-joined display text."""
    structured: List[Dict[str, str]] = []
    display_parts: List[str] = []
    for cr in creators or []:
        if not isinstance(cr, dict):
            continue
        ctype = (cr.get("creatorType") or "").strip().lower()
        if ctype != target_type:
            continue
        name_only = normalize_ws(cr.get("name"))
        first = normalize_ws(cr.get("firstName"))
        last = normalize_ws(cr.get("lastName"))
        if name_only:
            structured.append({"name": name_only})
            display_parts.append(name_only)
        elif last or first:
            structured.append({"first": first, "last": last})
            display_parts.append(f"{last}, {first}".strip(", ") if (last and first) else (last or first))
    return structured, "; ".join(display_parts)


def zotero_get_paginated(
    session: requests.Session,
    endpoint: str,
    max_retries: int = 4,
) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """Fetch all pages from a Zotero API v3 endpoint and return (records, library_version)."""
    all_rows: List[Dict[str, Any]] = []
    start = 0
    limit = 100
    lib_version: Optional[int] = None
    total_results: Optional[int] = None

    while True:
        params = {"limit": limit, "start": start}
        resp = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = session.get(endpoint, params=params, timeout=30)
                if resp.status_code == 200:
                    break
                if resp.status_code in (429, 500, 502, 503, 504):
                    retry_after = resp.headers.get("Retry-After") or resp.headers.get("Backoff")
                    wait_sec = int(retry_after) if (retry_after and str(retry_after).isdigit()) else attempt * 3
                    time.sleep(min(wait_sec, 30))
                    continue
                resp.raise_for_status()
            except requests.RequestException:
                if attempt == max_retries:
                    raise
                time.sleep(attempt * 2)

        if resp is None:
            break

        ver_hdr = resp.headers.get("Last-Modified-Version")
        if ver_hdr and str(ver_hdr).isdigit():
            lib_version = int(ver_hdr)
        tot_hdr = resp.headers.get("Total-Results")
        if tot_hdr and str(tot_hdr).isdigit():
            total_results = int(tot_hdr)

        batch = resp.json() or []
        if not batch:
            break
        all_rows.extend(batch)

        if len(batch) < limit or (total_results is not None and len(all_rows) >= total_results):
            break
        start += limit
        time.sleep(0.15)

    return all_rows, lib_version


def build_taxonomy_index(
    collections: List[Dict[str, Any]],
) -> Tuple[Dict[str, Tuple[str, str]], Dict[str, List[str]]]:
    """
    Dynamically build dimension -> subcollection mappings from Zotero collections.
    Supports both 2-level (`Dimension -> Leaf`) and 3-level (`Root -> Dimension -> Leaf`) trees.
    """
    by_key: Dict[str, Dict[str, Any]] = {}
    children_of: Dict[str, List[str]] = defaultdict(list)

    for col in collections:
        data = (col or {}).get("data") or {}
        key = data.get("key")
        if not key:
            continue
        parent = data.get("parentCollection") or ""
        by_key[key] = data
        children_of[parent if isinstance(parent, str) else ""].append(key)

    top_keys = children_of.get("", [])
    if len(top_keys) == 1 and any(children_of.get(ch) for ch in children_of.get(top_keys[0], [])):
        dim_keys = list(children_of.get(top_keys[0], []))
    else:
        dim_keys = [k for k in top_keys if children_of.get(k)] or list(top_keys)

    def _dim_sort_key(col_key: str) -> Tuple[int, str]:
        name = normalize_ws(by_key[col_key].get("name"))
        order_idx = PREFERRED_DIM_ORDER.index(name) if name in PREFERRED_DIM_ORDER else len(PREFERRED_DIM_ORDER)
        return (order_idx, name.lower())

    dim_keys.sort(key=_dim_sort_key)

    col_key_to_dim_sub: Dict[str, Tuple[str, str]] = {}
    taxonomy_tree: Dict[str, List[str]] = {}

    def _collect_leaves(root_dim_name: str, parent_key: str, acc: set) -> None:
        for ch_key in children_of.get(parent_key, []):
            ch_name = normalize_ws(by_key[ch_key].get("name"))
            if ch_name:
                col_key_to_dim_sub[ch_key] = (root_dim_name, ch_name)
                acc.add(ch_name)
            _collect_leaves(root_dim_name, ch_key, acc)

    for d_key in dim_keys:
        d_name = normalize_ws(by_key[d_key].get("name"))
        if not d_name:
            continue
        leaf_names: set = set()
        _collect_leaves(d_name, d_key, leaf_names)
        taxonomy_tree[d_name] = sorted(leaf_names, key=str.lower)

    return col_key_to_dim_sub, taxonomy_tree


def sanitize_item(
    raw_item: Dict[str, Any],
    col_key_to_dim_sub: Dict[str, Tuple[str, str]],
    taxonomy_tree: Dict[str, List[str]],
) -> Optional[Dict[str, Any]]:
    """Convert a raw Zotero item into a clean, copyright-compliant CC0/CC BY catalog entry."""
    data = (raw_item or {}).get("data") or {}
    item_type = normalize_ws(data.get("itemType"))
    if not item_type or item_type in ("note", "attachment", "annotation"):
        return None

    title = normalize_ws(data.get("title"))
    if not title:
        return None

    key = normalize_ws(data.get("key") or raw_item.get("key"))
    authors, author_display = format_creators(data.get("creators") or [], "author")
    editors, editor_display = format_creators(data.get("creators") or [], "editor")

    date_str = normalize_ws(data.get("date"))
    year = extract_year(date_str)

    venue = (
        normalize_ws(data.get("publicationTitle"))
        or normalize_ws(data.get("bookTitle"))
        or normalize_ws(data.get("proceedingsTitle"))
        or normalize_ws(data.get("university"))
        or normalize_ws(data.get("publisher"))
    )
    publisher = normalize_ws(data.get("publisher"))
    volume = normalize_ws(data.get("volume"))
    issue = normalize_ws(data.get("issue"))
    pages = normalize_ws(data.get("pages"))

    doi = clean_doi(data.get("DOI"))
    raw_url = normalize_ws(data.get("url"))
    url = f"https://doi.org/{doi}" if doi else raw_url

    tags = sorted(
        {
            normalize_ws(t.get("tag"))
            for t in (data.get("tags") or [])
            if isinstance(t, dict) and normalize_ws(t.get("tag"))
        },
        key=str.lower,
    )

    extra_meta = parse_extra_metadata(data.get("extra"))
    relevance = extra_meta["relevance"]
    if not relevance:
        if "rel:core" in tags:
            relevance = "core"
        elif "rel:case-study" in tags:
            relevance = "case_study"

    doc_type = extra_meta["doc_type"]
    if doc_type not in VALID_DOC_TYPES:
        doc_type = ""
        for t in tags:
            if t.startswith("type:") and t[5:] in VALID_DOC_TYPES:
                doc_type = t[5:]
                break
        if not doc_type:
            type_fallback = {
                "journalArticle": "research-article",
                "thesis": "thesis",
                "bookSection": "book-chapter",
                "conferencePaper": "conference-paper",
                "book": "book",
            }
            doc_type = type_fallback.get(item_type, "other")

    dims: Dict[str, List[str]] = {d_name: [] for d_name in taxonomy_tree.keys()}
    for c_key in data.get("collections") or []:
        if c_key in col_key_to_dim_sub:
            d_name, sub_name = col_key_to_dim_sub[c_key]
            if sub_name not in dims[d_name]:
                dims[d_name].append(sub_name)
    for d_name in dims:
        dims[d_name].sort(key=str.lower)

    return {
        "key": key,
        "item_type": item_type,
        "title": title,
        "authors": authors,
        "author_display": author_display,
        "editors": editors,
        "editor_display": editor_display,
        "year": year,
        "date": date_str,
        "venue": venue,
        "publisher": publisher,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "url": url,
        "relevance": relevance,
        "doc_type": doc_type,
        "citations": extra_meta["citations"],
        "dimensions": dims,
        "tags": tags,
        "ai_summary": extra_meta["ai_summary"],
    }


def compute_stats(
    items: List[Dict[str, Any]],
    taxonomy_tree: Dict[str, List[str]],
) -> Dict[str, Any]:
    """Compute aggregate coverage distributions dynamically from exported items."""
    by_item_type: Counter = Counter()
    by_relevance: Counter = Counter()
    by_doc_type: Counter = Counter()
    by_year_bucket: Counter = Counter()
    dim_counts: Dict[str, Dict[str, int]] = {
        d: {sub: 0 for sub in subs} for d, subs in taxonomy_tree.items()
    }
    recent_2024_plus = 0

    for it in items:
        by_item_type[it["item_type"] or "unknown"] += 1
        by_relevance[it["relevance"] or "unclassified"] += 1
        by_doc_type[it["doc_type"] or "other"] += 1
        yr = it.get("year")
        if isinstance(yr, int):
            if yr >= 2024:
                by_year_bucket["2024-2026"] += 1
                recent_2024_plus += 1
            elif yr >= 2020:
                by_year_bucket["2020-2023"] += 1
            else:
                by_year_bucket["<=2019"] += 1
        else:
            by_year_bucket["unknown"] += 1

        for d_name, subs in (it.get("dimensions") or {}).items():
            if d_name not in dim_counts:
                dim_counts[d_name] = {}
            for sub in subs:
                dim_counts[d_name][sub] = dim_counts[d_name].get(sub, 0) + 1

    return {
        "by_item_type": dict(sorted(by_item_type.items(), key=lambda x: (-x[1], x[0]))),
        "by_relevance": dict(sorted(by_relevance.items(), key=lambda x: (-x[1], x[0]))),
        "by_doc_type": dict(sorted(by_doc_type.items(), key=lambda x: (-x[1], x[0]))),
        "by_year_bucket": dict(by_year_bucket),
        "recent_2024_plus": recent_2024_plus,
        "dimensions": dim_counts,
    }


def render_csv(items: List[Dict[str, Any]], dim_names: List[str]) -> str:
    """Serialize sanitized catalog items into a UTF-8 CSV string."""
    import io

    buf = io.StringIO()
    dim_cols = [re.sub(r"[^a-z0-9]+", "_", d.lower()).strip("_") for d in dim_names]
    headers = [
        "zotero_key",
        "item_type",
        "title",
        "authors",
        "editors",
        "year",
        "venue",
        "publisher",
        "volume",
        "issue",
        "pages",
        "doi",
        "url",
        "relevance",
        "doc_type",
        "citations",
        *dim_cols,
        "tags",
        "ai_summary",
    ]
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(headers)

    for it in items:
        dims = it.get("dimensions") or {}
        row = [
            it.get("key", ""),
            it.get("item_type", ""),
            it.get("title", ""),
            it.get("author_display", ""),
            it.get("editor_display", ""),
            "" if it.get("year") is None else str(it["year"]),
            it.get("venue", ""),
            it.get("publisher", ""),
            it.get("volume", ""),
            it.get("issue", ""),
            it.get("pages", ""),
            it.get("doi", ""),
            it.get("url", ""),
            it.get("relevance", ""),
            it.get("doc_type", ""),
            "" if it.get("citations") is None else str(it["citations"]),
            *["; ".join(dims.get(d, [])) for d in dim_names],
            "; ".join(it.get("tags") or []),
            it.get("ai_summary", ""),
        ]
        writer.writerow(row)
    return buf.getvalue()


def should_skip_write(
    bib_json_path: Path,
    web_json_path: Path,
    bib_csv_path: Path,
    content_sha256: str,
    lib_version: Optional[int],
) -> bool:
    """Return True when existing exported artifacts already match content_sha256 and version."""
    if not (bib_json_path.exists() and web_json_path.exists() and bib_csv_path.exists()):
        return False
    try:
        prev = json.loads(bib_json_path.read_text(encoding="utf-8"))
        prev_meta = prev.get("meta") or {}
        if prev_meta.get("content_sha256") == content_sha256:
            if lib_version is None or prev_meta.get("zotero_library_version") == lib_version:
                return True
    except Exception:
        return False
    return False


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a public Zotero group or personal library into sanitized CC0/CC BY JSON & CSV catalog files."
    )
    parser.add_argument(
        "--group-id",
        default=os.getenv("ZOTERO_PUBLIC_GROUP_ID", "6777746"),
        help="Public Zotero Group ID to export (default: 6777746)",
    )
    parser.add_argument(
        "--user-id",
        default="",
        help="Optional personal Zotero User ID (overrides --group-id; requires --api-key or ZOTERO_API_KEY)",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="Optional Zotero API key (not required for public Zotero groups)",
    )
    parser.add_argument(
        "--group-url",
        default="",
        help="Optional canonical Zotero library URL embedded in exported metadata",
    )
    parser.add_argument(
        "--out-dir",
        default="data",
        help="Output directory for bibliography.json and bibliography.csv (default: data)",
    )
    parser.add_argument(
        "--web-data",
        default="docs/data/catalog.json",
        help="Output path for minified GitHub Pages JSON payload (default: docs/data/catalog.json)",
    )
    parser.add_argument(
        "--skip-if-unchanged",
        action="store_true",
        help="Skip rewriting files if content_sha256 and Zotero library version match the existing export",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    session = requests.Session()
    session.headers.update(
        {
            "Zotero-API-Version": "3",
            "User-Agent": "AC-Open-Bibliography-Exporter/1.0",
        }
    )

    api_key = (args.api_key or (os.getenv("ZOTERO_API_KEY", "") if args.user_id else "")).strip()
    if api_key:
        session.headers["Zotero-API-Key"] = api_key

    if args.user_id:
        lib_prefix = f"{ZOTERO_API_BASE}/users/{args.user_id.strip()}"
        source_id = args.user_id.strip()
        library_url = args.group_url or ""
    else:
        gid = str(args.group_id).strip()
        lib_prefix = f"{ZOTERO_API_BASE}/groups/{gid}"
        source_id = gid
        if args.group_url:
            library_url = args.group_url
        elif gid == "6777746":
            library_url = "https://www.zotero.org/groups/6777746/assassins_creed_research/library"
        else:
            library_url = f"https://www.zotero.org/groups/{gid}/library"

    print(f"Fetching collections from {lib_prefix}/collections ...")
    collections, col_ver = zotero_get_paginated(session, f"{lib_prefix}/collections")
    col_key_to_dim_sub, taxonomy_tree = build_taxonomy_index(collections)

    print(f"Fetching top-level items from {lib_prefix}/items/top ...")
    raw_items, item_ver = zotero_get_paginated(session, f"{lib_prefix}/items/top")
    lib_version = item_ver or col_ver

    sanitized: List[Dict[str, Any]] = []
    unique_tags: set = set()
    for raw_it in raw_items:
        clean = sanitize_item(raw_it, col_key_to_dim_sub, taxonomy_tree)
        if clean:
            sanitized.append(clean)
            unique_tags.update(clean["tags"])

    sanitized.sort(key=lambda x: (-(x["year"] or 0), x["title"].lower(), x["key"]))

    canonical_digest_input = json.dumps(
        {"taxonomy_tree": taxonomy_tree, "items": sanitized},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    content_sha256 = hashlib.sha256(canonical_digest_input).hexdigest()

    out_dir = Path(args.out_dir)
    bib_json_path = out_dir / "bibliography.json"
    bib_csv_path = out_dir / "bibliography.csv"
    web_json_path = Path(args.web_data)

    if args.skip_if_unchanged and should_skip_write(
        bib_json_path, web_json_path, bib_csv_path, content_sha256, lib_version
    ):
        print(
            f"Catalog unchanged (version=v{lib_version}, works={len(sanitized)}, sha256={content_sha256[:12]}); skipping write."
        )
        return 0

    dim_names = list(taxonomy_tree.keys())
    stats = compute_stats(sanitized, taxonomy_tree)
    leaf_collections = sum(len(v) for v in taxonomy_tree.values())
    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    payload = {
        "meta": {
            "generated_at": generated_at,
            "zotero_group_id": "" if args.user_id else source_id,
            "zotero_group_url": library_url,
            "zotero_library_version": lib_version,
            "total_works": len(sanitized),
            "total_collections": len(collections),
            "leaf_collections": leaf_collections,
            "total_tags": len(unique_tags),
            "content_sha256": content_sha256,
            "license_note": (
                "Bibliographic metadata is shared under CC0 1.0; curated 4D taxonomy labels "
                "and original English AI triage summaries are shared under CC BY 4.0. "
                "Publisher abstracts and full-text PDFs are excluded."
            ),
        },
        "taxonomy_tree": taxonomy_tree,
        "stats": stats,
        "items": sanitized,
    }

    pretty_json = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    min_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    csv_text = render_csv(sanitized, dim_names)

    atomic_write_text(bib_json_path, pretty_json)
    atomic_write_text(bib_csv_path, csv_text)
    atomic_write_text(web_json_path, min_json)

    print(
        f"Exported {len(sanitized)} works | {len(collections)} collections ({leaf_collections} leaves) | "
        f"{len(unique_tags)} tags | Zotero v{lib_version} | sha256={content_sha256[:12]}"
    )
    print(f"Wrote: {bib_json_path}, {bib_csv_path}, {web_json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
