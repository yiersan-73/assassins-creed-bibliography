"""
Config-Driven OpenAlex Academic Literature Pipeline
===================================================
Features:
- Externalized Domain & Taxonomy Configuration (domain_config.json + taxonomy.json)
- Stateful Query Pointer & Cooldown (search_state.json)
- Dynamic Entity-Perspective Query Matrix
- Citation Snowballing Engine (Fallback mechanism via OpenAlex cites API)
- Atomic File IO (Safe against disk corruption)
- Strict Academic Filter, Publication Venue Sanitizer & Crossref Biblio Enrichment
- Provider-Agnostic Multi-LLM Classification (OpenAI-Compatible & Anthropic) & Guardrails
"""

import datetime
import html
import json
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv

# Automatically load .env from the working directory
load_dotenv()

import requests
from pyzotero import zotero

# ==========================================
# 1. Configuration & Credential Isolation
# ==========================================
_RAW_LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "").strip().lower()
if not _RAW_LLM_PROVIDER:
    _RAW_LLM_PROVIDER = (
        "anthropic"
        if (os.getenv("ANTHROPIC_API_KEY") and not os.getenv("LLM_API_KEY") and not os.getenv("OPENAI_API_KEY"))
        else "openai"
    )
LLM_PROVIDER = _RAW_LLM_PROVIDER
LLM_API_KEY = (
    os.getenv("LLM_API_KEY")
    or os.getenv("OPENAI_API_KEY")
    or os.getenv("ANTHROPIC_API_KEY")
    or ""
).strip()
LLM_BASE_URL = (
    os.getenv("LLM_BASE_URL")
    or os.getenv("OPENAI_BASE_URL")
    or os.getenv("ANTHROPIC_BASE_URL")
    or ""
).strip().rstrip("/")
LLM_MODEL = (
    os.getenv("LLM_MODEL")
    or os.getenv("CLAUDE_MODEL")
    or ("claude-haiku-5-5" if LLM_PROVIDER == "anthropic" else "gpt-5.4-mini")
).strip()
CLAUDE_MODEL = LLM_MODEL  # Backward-compatible alias

ZOTERO_USER_ID = (os.getenv("ZOTERO_USER_ID") or "").strip()
ZOTERO_API_KEY = (os.getenv("ZOTERO_API_KEY") or "").strip()
ZOTERO_LIBRARY_TYPE = os.getenv("ZOTERO_LIBRARY_TYPE", "user")
OPENALEX_API_KEY = (os.getenv("OPENALEX_API_KEY") or "").strip()
ROOT_COLLECTION_KEY = (os.getenv("ZOTERO_ROOT_COLLECTION_KEY") or "").strip()
CACHE_FILE = "zotero_cache.json"
REJECTED_CACHE_FILE = "rejected_cache.json"
HISTORY_FILE = "batch_history.json"
STATE_FILE = "search_state.json"
TAXONOMY_FILE = os.getenv("TAXONOMY_FILE", "taxonomy.json")
DOMAIN_CONFIG_FILE = os.getenv("DOMAIN_CONFIG_FILE", "domain_config.json")

USER_EMAIL = os.getenv("USER_EMAIL", "academic_curator@example.com")


def _resolve_config_path(filename: str) -> str:
    """Resolve config file relative to CWD first, falling back to script directory."""
    if os.path.isabs(filename) or os.path.exists(filename):
        return filename
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(script_dir, filename)
    return candidate if os.path.exists(candidate) else filename


def load_domain_config(filepath: Optional[str] = None) -> Dict[str, Any]:
    """Load external domain_config.json controlling search scope, matrix, tags, and domain filters."""
    target = _resolve_config_path(filepath or DOMAIN_CONFIG_FILE)
    if os.path.exists(target):
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            print(f"⚠️ Failed to load {target} ({e}); using empty defaults.")
    return {}


def load_taxonomy(filepath: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """Load external taxonomy.json mapping dimension -> {subcollection_name: zotero_key}."""
    target = _resolve_config_path(filepath or TAXONOMY_FILE)
    if os.path.exists(target):
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            print(f"⚠️ Failed to load {target} ({e}); using empty taxonomy.")
    return {}


# ==========================================
# 2. Atomic JSON File Writer
# ==========================================
def atomic_json_write(filepath: str, data: Any) -> None:
    """Write JSON safely via NamedTemporaryFile + os.replace to prevent corruption on crash."""
    dir_name = os.path.dirname(os.path.abspath(filepath))
    with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        temp_name = tf.name
    os.replace(temp_name, filepath)


# ==========================================
# 3. Venue Sanitizer & Crossref DOI Recovery
# ==========================================
_LOADING_VARIANTS = {
    "loading",
    "loading...",
    "loading..",
    "loading ..",
    "loading...: the journal of the canadian game studies association",
}
_CANONICAL_PUB_ALIASES = {
    "games cult.": "Games and Culture",
    "trans. digit. games res. assoc.": "Transactions of the Digital Games Research Association",
}


def _canonicalize_pub_title(title: str) -> str:
    """Unescape HTML entities, collapse whitespace, and normalize known journal aliases."""
    cleaned = re.sub(r"\s+", " ", html.unescape(title)).strip()
    low = cleaned.lower()
    if low in _LOADING_VARIANTS:
        return "Loading...: The Journal of the Canadian Game Studies Association"
    if low in _CANONICAL_PUB_ALIASES:
        return _CANONICAL_PUB_ALIASES[low]
    return cleaned


_THESIS_UNIV_RE = re.compile(r"^.+?\(([^()]+)\)\s*$")
_THESIS_DASH_UNIV_RE = re.compile(r"^(?:diva|hal|dialnet|lirias|openaire)\s*[-–—:]\s*(.+)$", re.IGNORECASE)
_THESIS_UNIV_HINT_RE = re.compile(
    r"(?:university|universitat|universidade|universiteit|universität|université|"
    r"college|institute of technology|school of|polytechnic|hogeschool|academy)",
    re.IGNORECASE,
)
_NAME_SUFFIX_RE = {"jr", "sr", "dr", "st", "ed", "rev", "prof", "ii", "iii", "iv"}
_CROSSREF_CACHE: Dict[str, Dict[str, Any]] = {}


def _clean_creator_first_name(name: str) -> str:
    """Strip stray trailing periods from full given names (e.g., 'Kyleigh.' -> 'Kyleigh'), keeping initials and suffixes."""
    cleaned = re.sub(r"\s+", " ", (name or "")).strip()
    if cleaned.endswith("."):
        stem = cleaned[:-1].strip()
        tokens = [t for t in re.split(r"[\s.\-]+", stem) if t]
        if tokens and len(tokens[-1]) >= 2 and tokens[-1].lower() not in _NAME_SUFFIX_RE:
            return stem
    return cleaned


def _clean_thesis_university(venue: str) -> str:
    """Extract the awarding institution from thesis repository wrappers (e.g., 'DiVA (Stockholm University)' or 'DiVA - Jönköping University')."""
    cleaned = (venue or "").strip()
    m = _THESIS_UNIV_RE.match(cleaned)
    if m and _THESIS_UNIV_HINT_RE.search(m.group(1)):
        return m.group(1).strip()
    m_dash = _THESIS_DASH_UNIV_RE.match(cleaned)
    if m_dash and _THESIS_UNIV_HINT_RE.search(m_dash.group(1)):
        return m_dash.group(1).strip()
    return cleaned


def _clean_book_publisher(publisher: str) -> str:
    """Strip redundant 'eBooks' and 'Plc' suffixes from book publisher strings."""
    cleaned = _canonicalize_pub_title(publisher or "")
    cleaned = re.sub(r"\s+ebooks?\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s+plc\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def fetch_crossref_metadata(doi: str) -> Dict[str, Any]:
    """Fetch and cache container title, publisher, institution, biblio (vol/issue/pages), and editors from Crossref by DOI."""
    empty: Dict[str, Any] = {
        "container_title": "",
        "publisher": "",
        "institution": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "editors": [],
    }
    if not doi:
        return empty
    clean_doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "").strip().lower()
    if not clean_doi:
        return empty
    if clean_doi in _CROSSREF_CACHE:
        return _CROSSREF_CACHE[clean_doi]

    try:
        cr_url = f"https://api.crossref.org/works/{clean_doi}"
        resp = http_session.get(cr_url, timeout=6)
        if resp.status_code != 200:
            _CROSSREF_CACHE[clean_doi] = empty
            return empty
        msg = (resp.json() or {}).get("message", {}) or {}
    except Exception as e:
        print(f"❌ Crossref metadata lookup failed ({clean_doi}): {e}")
        return empty

    containers = msg.get("container-title") or []
    container_title = (
        containers[0].strip() if containers and isinstance(containers[0], str) else ""
    )
    pub_raw = msg.get("publisher") or ""
    publisher = pub_raw.strip() if isinstance(pub_raw, str) else ""
    insts = msg.get("institution") or []
    institution = (
        (insts[0].get("name") or "").strip()
        if insts and isinstance(insts[0], dict)
        else ""
    )
    volume = str(msg.get("volume") or "").strip()
    issue = str(msg.get("issue") or "").strip()
    pages = str(msg.get("page") or "").strip()

    editors: List[Dict[str, str]] = []
    for ed in (msg.get("editor") or []):
        if not isinstance(ed, dict):
            continue
        family = (ed.get("family") or "").strip()
        given = _clean_creator_first_name(ed.get("given") or "")
        name_only = (ed.get("name") or "").strip()
        if family:
            editors.append({"creatorType": "editor", "firstName": given, "lastName": family})
        elif name_only:
            editors.append({"creatorType": "editor", "name": name_only})

    result = {
        "container_title": container_title,
        "publisher": publisher,
        "institution": institution,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "editors": editors,
    }
    _CROSSREF_CACHE[clean_doi] = result
    return result


def sanitize_publication_title(
    raw_pub_title: str, doi: str = None, item_type: str = "journalArticle"
) -> str:
    """
    Sanitize publication venue fields (publicationTitle / bookTitle / proceedingsTitle / university / publisher).
    Preserves the peer-reviewed journal 'Loading...' (ISSN: 1923-2691), unescapes HTML entities,
    filters invalid placeholders/eBook wrappers/aggregators, and attempts Crossref DOI recovery.
    """
    if not raw_pub_title or not isinstance(raw_pub_title, str):
        cleaned_title = ""
    else:
        cleaned_title = _canonicalize_pub_title(raw_pub_title)

    # 1. Legitimate journal exception: 'Loading...' is the Canadian Game Studies Association journal
    if cleaned_title == "Loading...: The Journal of the Canadian Game Studies Association":
        return cleaned_title

    # 2. Detect dirty placeholders, eBook wrappers, or aggregator names and recover via Crossref DOI
    invalid_placeholders = {
        "null", "undefined", "none", "n/a", "unknown", "untitled", "placeholder", "nan", "-"
    }
    aggregator_prefixes = (
        "doaj (", "la referencia", "dialnet", "dergipark", "cairn.info", "zenodo", "hal", "diva ", "diva -"
    )

    low_title = cleaned_title.lower()
    is_invalid = (not cleaned_title) or (low_title in invalid_placeholders)
    is_ebook_wrapper = "ebook" in low_title
    is_aggregator = low_title == "hal" or any(low_title.startswith(p) for p in aggregator_prefixes)

    if is_invalid or is_ebook_wrapper or is_aggregator:
        if low_title in invalid_placeholders:
            print(f"⚠️ Filtered invalid venue placeholder: '{raw_pub_title}'")
        if doi:
            cr_meta = fetch_crossref_metadata(doi)
            if item_type == "book":
                pub_raw = cr_meta.get("publisher") or ""
                if pub_raw:
                    real_pub = _clean_book_publisher(pub_raw)
                    if real_pub and real_pub.lower() not in invalid_placeholders:
                        print(f"✅ Recovered book publisher from Crossref: '{real_pub}'")
                        return real_pub
            elif item_type == "thesis":
                cand = cr_meta.get("institution") or cr_meta.get("container_title") or ""
                if cand:
                    real_univ = _clean_thesis_university(_canonicalize_pub_title(cand))
                    if real_univ and real_univ.lower() not in invalid_placeholders:
                        print(f"✅ Recovered awarding university from Crossref: '{real_univ}'")
                        return real_univ
            else:
                cand = cr_meta.get("container_title") or ""
                if cand:
                    real_pub = _canonicalize_pub_title(cand)
                    if (
                        real_pub
                        and real_pub.lower() not in invalid_placeholders
                        and "ebook" not in real_pub.lower()
                    ):
                        print(f"✅ Recovered publication venue from Crossref: '{real_pub}'")
                        return real_pub

        if item_type == "book" and is_ebook_wrapper:
            return _clean_book_publisher(cleaned_title)
        if item_type == "thesis" and not is_invalid:
            return _clean_thesis_university(cleaned_title)
        return "" if is_invalid else cleaned_title

    if item_type == "thesis":
        return _clean_thesis_university(cleaned_title)

    return cleaned_title

# ==========================================
# 4. Dynamic Domain & Taxonomy Runtime Config
# ==========================================
def normalize_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"[’'‘`\u2019\u2018]", "'", text.lower()).strip()


def _slugify_tag_part(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "other"


def build_openalex_base_filter(search_phrases: List[str]) -> str:
    """Build OpenAlex Stage-1 filter string dynamically from domain search phrases."""
    clean_phrases = [str(p).strip() for p in (search_phrases or []) if str(p).strip()]
    base_parts = ["has_abstract:true"]
    if clean_phrases:
        phrase_expr = "|".join(f'"{p}"' for p in clean_phrases)
        base_parts.append(f"title_and_abstract.search:{phrase_expr}")
    base_parts.append(
        "type:!paratext,type:!editorial,type:!erratum,type:!dataset,type:!retraction,type:!other"
    )
    return ",".join(base_parts)


def build_system_prompt(
    domain_cfg: Dict[str, Any],
    taxonomy: Dict[str, Dict[str, str]],
    generic_other_keys: Dict[str, str],
) -> str:
    """Construct LLM classification prompt dynamically from domain_config and taxonomy."""
    topic_name = str(domain_cfg.get("topic_name") or "Target Academic Topic").strip()
    topic_domain = str(domain_cfg.get("topic_domain") or f"{topic_name} studies").strip()

    custom_rules = domain_cfg.get("custom_prompt_rules") or []
    custom_rules_block = ("\n" + "\n".join(str(r) for r in custom_rules) + "\n") if custom_rules else "\n"

    lang_map = taxonomy.get("Languages") or {}
    lang_en_key = lang_map.get("English", "")
    lang_other_key = lang_map.get("Other languages", "")
    lang_rules_lines = []
    if lang_map:
        lang_rules_lines.append('PRIMARY RULES FOR "Languages" CATEGORY:')
        en_hint = (
            f' (e.g., an English paper studying non-English localization MUST be assigned "{lang_en_key}" English'
            + (f', NOT "{lang_other_key}"' if lang_other_key else "")
            + ")"
            if lang_en_key
            else ""
        )
        lang_rules_lines.append(
            "1. Classify strictly by the LANGUAGE THE PAPER IS WRITTEN IN, NOT the language being studied or localized in the paper"
            + en_hint
            + "."
        )
        lang_rules_lines.append(
            "2. If the abstract is provided in English while the title is in another language (e.g., Spanish, Portuguese, French, German), classify by the original writing language indicated by the title."
        )
        lang_rules_lines.append(
            "3. Conversely, if the title is in English (e.g., an English-department thesis or a review of an English monograph) while the abstract is clearly written in another language or begins with non-English academic headers (such as INTISARI, ABSTRAK, RESUMEN, RESUMO, RÉSUMÉ, ZUSAMMENFASSUNG, STRESZCZENIE, RIASSUNTO), classify strictly by the language of the abstract/body, NOT English."
        )
    lang_rules_block = ("\n" + "\n".join(lang_rules_lines) + "\n") if lang_rules_lines else ""

    rel_ex = domain_cfg.get("relevance_examples") or {}
    core_desc = rel_ex.get(
        "core",
        f"{topic_name} (its core works, major features, or direct adaptations) is the primary subject of the paper.",
    )
    case_desc = rel_ex.get(
        "case_study",
        f"The paper explores a broader theme AND explicitly analyzes {topic_name} as a substantial case study or comparative object.",
    )
    passing_desc = rel_ex.get(
        "passing_mention",
        f"{topic_name} is ONLY mentioned briefly as background context or inside an illustrative list with NO substantive analysis.",
    )

    dim_names = list(taxonomy.keys()) or ["Dimensions"]
    dim_list_str = ", ".join(f'"{d}"' for d in dim_names)
    key_placeholders = [f'"KEY_FROM_{re.sub(r"[^A-Z0-9]+", "_", d.upper()).strip("_")}"' for d in dim_names]
    fallback_list = ", ".join(f'"{v}"' for v in generic_other_keys.values())

    topics_rule = (
        '2. Assign ALL directly applicable "Topics" subcollections (typically 1 to 3) rather than stopping at only one when a paper substantively spans multiple themes.\n'
        if "Topics" in taxonomy
        else "2. Assign all directly applicable subcollections across each category.\n"
    )
    fallback_rule = (
        f"4. Do NOT include fallback keys ({fallback_list}) when a more specific subcollection in that category already applies.\n"
        if fallback_list
        else "4. Do NOT include generic fallback keys when a more specific subcollection in that category already applies.\n"
    )

    return f"""You are an academic curator specializing strictly in {topic_domain}.
Analyze the provided paper title and abstract, then assign the paper to appropriate subcollections based on the structured Taxonomy below.

TAXONOMY STRUCTURE:
{json.dumps(taxonomy, indent=2)}
{custom_rules_block}{lang_rules_block}
RELEVANCE CLASSIFICATION ("relevance"):
Classify how central {topic_name} is to this paper as ONE of:
- "core": {core_desc}
- "case_study": {case_desc}
- "passing_mention": {passing_desc}

DOCUMENT TYPE CLASSIFICATION ("doc_type"):
Classify the academic genre using Title, Abstract, Venue, and OpenAlex Type as ONE of:
"research-article", "book-chapter", "thesis", "conference-paper", "book", "book-review", "other".
(Hints: institutional repositories like DiVA/Utrecht/HAL/Lirias/Pure/Dialnet/DergiPark often index theses or conference papers under OpenAlex type "article"—check abstract/venue cues for thesis/dissertation wording -> "thesis", or conference/proceedings wording -> "conference-paper"; publisher eBook chapters -> "book-chapter"; titles ending with "(review)" or starting with "review of" or "Book Review" -> "book-review".)

CRITICAL RULES:
1. You MUST select AT LEAST ONE key from EACH of the {len(dim_names)} categories ({dim_list_str}).
{topics_rule}3. REGARDLESS of the original language of the paper, the "one_sentence_summary" MUST strictly be written in ENGLISH.
4. Output ONLY the 8-character Collection Keys in "collection_keys".
{fallback_rule}5. Do NOT emit "rel:*" or "type:*" tags in "tags" (they are generated deterministically from "relevance" and "doc_type").

OUTPUT FORMAT REQUIREMENTS:
Output STRICT JSON ONLY.
{{
  "relevance": "core",
  "doc_type": "research-article",
  "collection_keys": [{", ".join(key_placeholders)}],
  "tags": ["topic-keyword-1", "topic-keyword-2"],
  "one_sentence_summary": "A concise English summary highlighting the main argument."
}}
"""


def build_runtime_config(
    domain_cfg: Dict[str, Any],
    taxonomy: Dict[str, Dict[str, str]],
) -> Dict[str, Any]:
    """Derive all runtime lookup maps, defaults, filters, tag mappings, and prompts from config + taxonomy."""
    name_to_key_map: Dict[str, str] = {}
    for category_map in taxonomy.values():
        for name, key in category_map.items():
            name_to_key_map[str(name).lower()] = key
            name_to_key_map[str(key).lower()] = key

    cfg_defaults = domain_cfg.get("taxonomy_defaults") or {}
    defaults: Dict[str, str] = {}
    for dim_name, dim_map in taxonomy.items():
        pref = cfg_defaults.get(dim_name)
        resolved = ""
        if pref:
            if pref in dim_map.values():
                resolved = pref
            elif pref in dim_map:
                resolved = dim_map[pref]
            elif str(pref).lower() in name_to_key_map:
                resolved = name_to_key_map[str(pref).lower()]
        if not resolved and dim_map:
            resolved = next(iter(dim_map.values()))
        if resolved:
            defaults[dim_name] = resolved

    cfg_generic_other = domain_cfg.get("generic_other_categories") or {}
    generic_other_keys: Dict[str, str] = {}
    for dim_name, ref in cfg_generic_other.items():
        dim_map = taxonomy.get(dim_name) or {}
        if ref in dim_map.values():
            generic_other_keys[dim_name] = ref
        elif ref in dim_map:
            generic_other_keys[dim_name] = dim_map[ref]

    tag_dim_cfg = domain_cfg.get("tag_dimensions") or {}
    dimension_tag_maps: List[Tuple[str, Dict[str, str]]] = []
    for dim_name, spec in tag_dim_cfg.items():
        dim_map = taxonomy.get(dim_name) or {}
        if not dim_map:
            continue
        spec_dict = spec if isinstance(spec, dict) else {}
        prefix = str(spec_dict.get("prefix") or _slugify_tag_part(dim_name)).strip()
        aliases = spec_dict.get("aliases") or {}
        key_map: Dict[str, str] = {}
        for sub_name, sub_key in dim_map.items():
            slug = str(aliases.get(sub_name) or _slugify_tag_part(sub_name)).strip()
            key_map[sub_key] = f"{prefix}:{slug}"
        dimension_tag_maps.append((dim_name, key_map))

    search_phrases = [str(p).strip() for p in (domain_cfg.get("search_phrases") or []) if str(p).strip()]
    search_phrases_norm = [normalize_text(p) for p in search_phrases if normalize_text(p)]
    topic_name = str(domain_cfg.get("topic_name") or "Target Topic").strip()
    entities = [str(e).strip() for e in (domain_cfg.get("entities") or ([topic_name] if topic_name else [])) if str(e).strip()]
    perspectives = [str(p).strip() for p in (domain_cfg.get("perspectives") or []) if str(p).strip()]
    seed_queries = [str(q).strip() for q in (domain_cfg.get("seed_queries") or ([topic_name] if topic_name else [])) if str(q).strip()]

    return {
        "topic_name": topic_name,
        "topic_domain": str(domain_cfg.get("topic_domain") or f"{topic_name} studies").strip(),
        "bot_name": str(domain_cfg.get("bot_name") or "Academic_Literature_Bot/1.0").strip(),
        "search_phrases": search_phrases,
        "search_phrases_norm": search_phrases_norm,
        "prefix_strip_regex": str(domain_cfg.get("prefix_strip_regex") or "").strip(),
        "seed_queries": seed_queries,
        "entities": entities,
        "perspectives": perspectives,
        "openalex_base_filter": build_openalex_base_filter(search_phrases),
        "name_to_key_map": name_to_key_map,
        "defaults": defaults,
        "generic_other_keys": generic_other_keys,
        "dimension_tag_maps": dimension_tag_maps,
        "blocked_authors": {str(a).lower().strip() for a in (domain_cfg.get("blocked_authors") or []) if str(a).strip()},
        "blocked_venues": [str(v).lower().strip() for v in (domain_cfg.get("blocked_venues") or []) if str(v).strip()],
        "blocked_title_keywords": [str(k).lower().strip() for k in (domain_cfg.get("blocked_title_keywords") or []) if str(k).strip()],
        "blocked_asset_regexes": [str(r) for r in (domain_cfg.get("blocked_asset_regexes") or []) if str(r).strip()],
        "system_prompt": build_system_prompt(domain_cfg, taxonomy, generic_other_keys),
    }


DOMAIN_CONFIG: Dict[str, Any] = load_domain_config()
TAXONOMY: Dict[str, Dict[str, str]] = load_taxonomy()
_RUNTIME = build_runtime_config(DOMAIN_CONFIG, TAXONOMY)

TOPIC_NAME: str = _RUNTIME["topic_name"]
TOPIC_DOMAIN: str = _RUNTIME["topic_domain"]
BOT_NAME: str = _RUNTIME["bot_name"]
SEARCH_PHRASES: List[str] = _RUNTIME["search_phrases"]
SEARCH_PHRASES_NORM: List[str] = _RUNTIME["search_phrases_norm"]
PREFIX_STRIP_REGEX: str = _RUNTIME["prefix_strip_regex"]
SEED_QUERIES: List[str] = _RUNTIME["seed_queries"]
ENTITIES: List[str] = _RUNTIME["entities"]
PERSPECTIVES: List[str] = _RUNTIME["perspectives"]
DEFAULTS: Dict[str, str] = _RUNTIME["defaults"]
GENERIC_OTHER_KEYS: Dict[str, str] = _RUNTIME["generic_other_keys"]
NAME_TO_KEY_MAP: Dict[str, str] = _RUNTIME["name_to_key_map"]
DIMENSION_TAG_MAPS: List[Tuple[str, Dict[str, str]]] = _RUNTIME["dimension_tag_maps"]
GAME_KEY_TO_TAG: Dict[str, str] = dict(next((mp for d, mp in DIMENSION_TAG_MAPS if d == "Games"), {}))
LANG_KEY_TO_TAG: Dict[str, str] = dict(next((mp for d, mp in DIMENSION_TAG_MAPS if d == "Languages"), {}))
BLOCKED_AUTHORS: Set[str] = _RUNTIME["blocked_authors"]
BLOCKED_VENUES: List[str] = _RUNTIME["blocked_venues"]
BLOCKED_TITLE_KEYWORDS: List[str] = _RUNTIME["blocked_title_keywords"]
BLOCKED_ASSET_REGEXES: List[str] = _RUNTIME["blocked_asset_regexes"]

# Initialize API clients & isolated HTTP sessions
_anthropic_client = None
zot = zotero.Zotero(ZOTERO_USER_ID or "0", ZOTERO_LIBRARY_TYPE, ZOTERO_API_KEY or "")

# Dedicated LLM HTTP session (credential isolation for OpenAI-compatible APIs)
llm_session = requests.Session()
llm_session.headers.update({"User-Agent": f"{BOT_NAME} (mailto:{USER_EMAIL})"})

# Crossref / general HTTP session (isolated from OpenAlex and LLM credentials)
http_session = requests.Session()
http_session.headers.update({"User-Agent": f"{BOT_NAME} (mailto:{USER_EMAIL})"})

# Dedicated OpenAlex session (credential isolation: sends Bearer token only to api.openalex.org)
openalex_session = requests.Session()
openalex_session.headers.update({"User-Agent": f"{BOT_NAME} (mailto:{USER_EMAIL})"})
if OPENALEX_API_KEY:
    openalex_session.headers.update({"Authorization": f"Bearer {OPENALEX_API_KEY}"})


def build_canonical_tags(
    relevance: str,
    doc_type: str,
    collections: List[str],
    source_tag: str = "#source/openalex",
) -> List[Dict[str, str]]:
    """Build controlled canonical Zotero tags (rel:*, type:*, configured dimension tags, #source/*)."""
    norm_rel = normalize_relevance(relevance)
    rel_tag = "rel:core" if norm_rel == "core" else "rel:case-study"
    clean_doc_type = doc_type if doc_type in VALID_DOC_TYPES else "other"

    col_list = collections or []
    dim_tags: List[str] = []
    for _, key_map in DIMENSION_TAG_MAPS:
        dim_tags.extend(sorted({key_map[c] for c in col_list if c in key_map}))

    clean_source = (source_tag or "#source/openalex").strip() or "#source/openalex"

    ordered = [rel_tag, f"type:{clean_doc_type}"] + dim_tags + [clean_source]
    seen = set()
    result = []
    for t in ordered:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            result.append({"tag": t})
    return result


def reconstruct_openalex_abstract(inverted_index: Optional[dict]) -> str:
    if not inverted_index:
        return ""
    position_map = {}
    for word, positions in inverted_index.items():
        for pos in positions:
            position_map[pos] = word
    sorted_positions = sorted(position_map.keys())
    return " ".join([position_map[pos] for pos in sorted_positions])


# ==========================================
# 5. Dynamic Query Matrix & Cursor State
# ==========================================
def generate_dynamic_queries() -> List[str]:
    queries = list(SEED_QUERIES)
    for e in ENTITIES:
        for p in PERSPECTIVES:
            combo = f"{e} {p}"
            if combo not in queries:
                queries.append(combo)
    return queries


def load_search_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"current_index": 0, "dry_queries": {}}


def save_search_state(next_index: int, dry_queries: dict) -> None:
    state_data = {
        "current_index": next_index,
        "dry_queries": dry_queries,
        "last_run": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    atomic_json_write(STATE_FILE, state_data)


# ==========================================
# 6. Strict Academic Pre-Filter Pipeline
# ==========================================
def is_strictly_academic(work_data: dict, abstract: str) -> bool:
    if not isinstance(work_data, dict):
        return False
    raw_title = work_data.get("display_name") or work_data.get("title") or ""
    title = raw_title if isinstance(raw_title, str) else str(raw_title)
    title_lower = title.lower().strip()
    if not title_lower:
        return False
    abstract_lower = abstract.lower().strip() if isinstance(abstract, str) else ""

    work_type = (work_data.get("type") or "").lower()
    if work_type in ["dataset", "other", "editorial", "erratum", "paratext", "retraction"]:
        return False

    primary_loc = work_data.get("primary_location") or {}
    if not isinstance(primary_loc, dict):
        primary_loc = {}
    source = primary_loc.get("source") or {}
    if not isinstance(source, dict):
        source = {}
    raw_venue = source.get("display_name") or ""
    venue_lower = (raw_venue if isinstance(raw_venue, str) else str(raw_venue)).lower()
    if BLOCKED_VENUES and any(bv in venue_lower for bv in BLOCKED_VENUES):
        return False

    if BLOCKED_AUTHORS:
        for authorship in (work_data.get("authorships") or []):
            if not isinstance(authorship, dict):
                continue
            author_obj = authorship.get("author") or {}
            if not isinstance(author_obj, dict):
                continue
            raw_author = author_obj.get("display_name") or ""
            author_name = (raw_author if isinstance(raw_author, str) else str(raw_author)).lower().strip()
            if author_name in BLOCKED_AUTHORS:
                return False

    exact_structural_junk = {
        "mediography",
        "selected bibliography",
        "bibliography",
        "filmography",
        "ludography",
        "works cited",
        "list of figures",
        "list of tables",
        "index",
        "abbildungs- und tabellenverzeichnis",
        "tabellenverzeichnis",
        "abbildungsverzeichnis",
        "selección bibliográfica",
        "seleccion bibliografica",
        "bibliografía",
        "bibliografia",
        "literaturverzeichnis",
    }
    journal_structural_junk = [
        "front cover", "back cover", "table of contents", "editorial board", 
        "author index", "subject index", "issue information", "inside front cover",
        "selected bibliography",
    ]
    if (
        title_lower in exact_structural_junk
        or title_lower in journal_structural_junk
        or any(title_lower.startswith(j) for j in journal_structural_junk)
    ):
        return False

    if "gesamt pdf" in title_lower or "gesamt-pdf" in title_lower:
        return False

    asset_patterns = [
        r"\bobjaverse\b",
        r"\bsketchfab\b",
        r"\bmesh asset\b",
        r"\b3d asset\b",
        r"\bgame asset\b",
        r"\bblender\s+(?:file|model|asset)\b",
        r"\b(?:free\s+)?3d\s+models?\s+(?:download|asset|free|pack)\b",
    ] + BLOCKED_ASSET_REGEXES
    if (
        any(re.search(p, title_lower) or re.search(p, abstract_lower) for p in asset_patterns)
        or title_lower.endswith(" - 3d model")
    ):
        return False

    student_keywords = ["9th grade", "level 4", "student project", "homework", "coursework", "high school"]
    if any(k in title_lower for k in student_keywords):
        return False

    universal_seo_keywords = [
        "best mobile apps", "top 10", "top 5", "mobile apps for", "download for",
        "free download", "apk download", "cheats and hints", "walkthrough guide",
        "system requirements", "how to download", "best games like",
        "startups that'll change", "case study you'll never forget",
        "you'll never forget",
    ] + BLOCKED_TITLE_KEYWORDS
    if any(k in title_lower for k in universal_seo_keywords):
        return False

    if re.search(r"\b\d+\s+(startups|tips|ways|reasons|tricks)\s+that(?:'ll| will)\b", title_lower):
        return False

    clean_abstract = re.sub(r"\s+", " ", abstract_lower)
    if len(clean_abstract) < 120:
        return False

    return True


# ==========================================
# 7. Multi-Dimensional Prompt & Helpers
# ==========================================
SYSTEM_PROMPT = _RUNTIME["system_prompt"]


VALID_DOC_TYPES = {
    "research-article", "book-chapter", "thesis",
    "conference-paper", "book", "book-review", "other",
}
VALID_RELEVANCES = {"core", "case_study", "passing_mention"}


def normalize_doc_type(ai_doc_type: str, work_type: str = "", source_type: str = "") -> str:
    """Normalize document type across AI classification and OpenAlex work/source metadata."""
    cleaned = re.sub(r"[\s_]+", "-", (ai_doc_type or "").strip().lower())
    wt = (work_type or "").strip().lower()
    st = (source_type or "").strip().lower()

    specific_open_types = (
        "dissertation", "book-chapter", "proceedings-article", "conference-paper",
        "book", "monograph", "edited-book", "article", "review", "preprint",
    )
    if cleaned in VALID_DOC_TYPES and not (cleaned == "other" and wt in specific_open_types):
        return cleaned

    if wt == "dissertation":
        return "thesis"
    if wt == "book-chapter":
        return "book-chapter"
    if wt in ("proceedings-article", "conference-paper") or st == "conference":
        return "conference-paper"
    if wt in ("book", "monograph", "edited-book"):
        return "book"
    if wt in ("article", "review", "preprint"):
        return "research-article"
    return "research-article" if st == "journal" else "other"


def resolve_zotero_item_type(doc_type: str, work_type: str = "", source_type: str = "") -> str:
    """Map normalized doc_type / OpenAlex workType to native Zotero itemType."""
    wt = (work_type or "").strip().lower()
    st = (source_type or "").strip().lower()
    if doc_type == "thesis" or wt == "dissertation":
        return "thesis"
    if doc_type == "book-chapter" or wt == "book-chapter":
        return "bookSection"
    if doc_type == "conference-paper" or wt in ("proceedings-article", "conference-paper") or st == "conference":
        return "conferencePaper"
    if doc_type == "book" or wt in ("book", "monograph", "edited-book"):
        return "book"
    return "journalArticle"


def normalize_relevance(raw_rel: str) -> str:
    """Normalize relevance label into one of (core, case_study, passing_mention)."""
    cleaned = re.sub(r"[\s\-]+", "_", (raw_rel or "").strip().lower())
    return cleaned if cleaned in VALID_RELEVANCES else "case_study"


_GENERIC_ABSTRACT_LANG_PREFIXES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^\s*(?:intisari|abstrak)\b", re.IGNORECASE), "Indonesian"),
    (re.compile(r"^\s*resumen\b", re.IGNORECASE), "Spanish"),
    (re.compile(r"^\s*resumo\b", re.IGNORECASE), "Portuguese"),
    (re.compile(r"^\s*r[ée]sum[ée]\b", re.IGNORECASE), "French"),
    (re.compile(r"^\s*zusammenfassung\b", re.IGNORECASE), "German"),
    (re.compile(r"^\s*streszczenie\b", re.IGNORECASE), "Polish"),
    (re.compile(r"^\s*riassunto\b", re.IGNORECASE), "Italian"),
]


def apply_generic_language_guardrail(abstract: str, collection_keys: List[str]) -> List[str]:
    """Domain-agnostic language guardrail: resolves non-English abstract prefixes via TAXONOMY['Languages']."""
    head = (abstract or "")[:80]
    if not head:
        return list(collection_keys or [])

    lang_map = TAXONOMY.get("Languages") or {}
    target_key = None
    for pattern, lang_name in _GENERIC_ABSTRACT_LANG_PREFIXES:
        if pattern.search(head):
            target_key = lang_map.get(lang_name) or lang_map.get("Other languages")
            break

    if not target_key:
        return list(collection_keys or [])

    eng_key = lang_map.get("English")
    updated = [
        k for k in (collection_keys or [])
        if str(k).strip().lower() != (eng_key or "").lower()
    ]
    if target_key not in updated:
        updated.append(target_key)
    return updated


def enforce_four_categories(
    selected_keys: list,
    taxonomy: Optional[Dict[str, Dict[str, str]]] = None,
    defaults: Optional[Dict[str, str]] = None,
    generic_other_keys: Optional[Dict[str, str]] = None,
    name_to_key_map: Optional[Dict[str, str]] = None,
) -> list:
    """Enforce at least one valid collection key per taxonomy dimension and strip redundant generic fallbacks."""
    tax = TAXONOMY if taxonomy is None else taxonomy
    defs = DEFAULTS if defaults is None else defaults
    gen_other = GENERIC_OTHER_KEYS if generic_other_keys is None else generic_other_keys
    n2k = NAME_TO_KEY_MAP if name_to_key_map is None else name_to_key_map

    resolved_keys = set()
    for raw_key in (selected_keys or []):
        k_clean = str(raw_key).strip().lower()
        if k_clean in n2k:
            resolved_keys.add(n2k[k_clean])
        else:
            resolved_keys.add(raw_key)

    for category_name, category_map in tax.items():
        category_keys = set(category_map.values())
        matched = resolved_keys & category_keys
        if not matched:
            if category_name in defs:
                resolved_keys.add(defs[category_name])
        elif category_name in gen_other:
            other_key = gen_other[category_name]
            if other_key in matched and len(matched) > 1:
                resolved_keys.discard(other_key)

    return list(resolved_keys)


# ==========================================
# 8. Zotero Fingerprint & Cache Sync
# ==========================================
def get_existing_zotero_cache(collection_key: str) -> Tuple[Set[str], Set[str]]:
    cache_data = {"last_version": 0, "titles": [], "dois": []}

    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
        except Exception:
            pass

    existing_titles = set(cache_data.get("titles", []))
    existing_dois = set(cache_data.get("dois", []))
    last_version = cache_data.get("last_version", 0)

    print(f"📦 Loading local fingerprint cache ({len(existing_titles)} titles, last version: v{last_version})...")

    try:
        cloud_version = zot.last_modified_version()

        if last_version > 0 and cloud_version == last_version:
            print(f"⚡ [Fast Check] Zotero cloud unchanged (v{cloud_version}); local cache is 100% up to date!\n")
            return existing_titles, existing_dois

        if last_version == 0:
            print("🔄 Building initial paginated Zotero fingerprint index...")
            start, limit = 0, 100
            while True:
                items = zot.collection_items(collection_key, start=start, limit=limit)
                if not items:
                    break
                for item in items:
                    data = item.get("data", {})
                    t = normalize_text(data.get("title", ""))
                    d = data.get("DOI", "").strip().lower()
                    if t: existing_titles.add(t)
                    if d: existing_dois.add(d)
                if len(items) < limit:
                    break
                start += limit
        else:
            print(f"📡 Cloud version changed (v{last_version} -> v{cloud_version}); syncing incremental updates...")
            start, limit = 0, 100
            while True:
                changed_items = zot.collection_items(
                    collection_key, since=last_version, start=start, limit=limit
                )
                if not changed_items:
                    break
                for item in changed_items:
                    data = item.get("data", {})
                    t = normalize_text(data.get("title", ""))
                    d = data.get("DOI", "").strip().lower()
                    if t: existing_titles.add(t)
                    if d: existing_dois.add(d)
                if len(changed_items) < limit:
                    break
                start += limit

        atomic_json_write(
            CACHE_FILE,
            {
                "last_version": cloud_version,
                "titles": list(existing_titles),
                "dois": list(existing_dois),
            },
        )
 
        print(f"✅ Fingerprint cache synced: {len(existing_titles)} total titles (version: v{cloud_version})\n")
        return existing_titles, existing_dois

    except Exception as e:
        print(f"⚠️ Cloud version check failed ({e}); falling back to offline deduplication with {len(existing_titles)} titles...\n")
        return existing_titles, existing_dois


def save_to_local_cache(
    new_titles: list, new_dois: list, cloud_version: Optional[int] = None
) -> None:
    cache_data = {"last_version": 0, "titles": [], "dois": []}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
        except Exception:
            pass

    titles_set = set(cache_data.get("titles", []))
    dois_set = set(cache_data.get("dois", []))

    for t in new_titles:
        if t: titles_set.add(normalize_text(t))
    for d in new_dois:
        if d: dois_set.add(d.strip().lower())

    final_version = cloud_version if cloud_version is not None else cache_data.get("last_version", 0)
    atomic_json_write(
        CACHE_FILE,
        {
            "last_version": final_version,
            "titles": list(titles_set),
            "dois": list(dois_set),
        },
    )


def load_rejected_cache() -> Tuple[Set[str], Set[str]]:
    """Load rejected cache (passing_mention items) to avoid re-fetching or wasting LLM tokens."""
    if os.path.exists(REJECTED_CACHE_FILE):
        try:
            with open(REJECTED_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data.get("titles", [])), set(data.get("dois", []))
        except Exception:
            pass
    return set(), set()


def save_rejected_cache(new_records: List[dict]) -> None:
    """Atomically append intercepted passing_mention titles, DOIs, and summaries to rejected_cache.json."""
    if not new_records:
        return
    data = {"titles": [], "dois": [], "records": []}
    if os.path.exists(REJECTED_CACHE_FILE):
        try:
            with open(REJECTED_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass

    titles_set = set(data.get("titles", []))
    dois_set = set(data.get("dois", []))
    records = list(data.get("records", []))

    for rec in new_records:
        t = rec.get("title", "")
        d = (rec.get("doi") or "").strip().lower()
        if t:
            titles_set.add(normalize_text(t))
        if d:
            dois_set.add(d)
        records.append(rec)

    atomic_json_write(
        REJECTED_CACHE_FILE,
        {
            "titles": list(titles_set),
            "dois": list(dois_set),
            "records": records,
        },
    )


# ==========================================
# 9. OpenAlex Search & Citation Snowballing Engine
# ==========================================
class OpenAlexQuotaExceededError(Exception):
    """Raised when OpenAlex daily budget is exhausted or 429 pacing limits persist."""

    def __init__(self, message: str, partial_papers: Optional[List[dict]] = None):
        super().__init__(message)
        self.partial_papers = list(partial_papers) if partial_papers else []


class OpenAlexRequestError(Exception):
    """Raised on transient OpenAlex HTTP/network failure (must not pollute dry_queries)."""


_OPENALEX_QUOTA_EXCEEDED = False

OPENALEX_BASE_FILTER = _RUNTIME["openalex_base_filter"]


def _parse_float_header(headers: Any, key: str) -> Optional[float]:
    """Safely parse float rate-limit headers from HTTP responses."""
    if not headers:
        return None
    raw = headers.get(key)
    if raw is None:
        return None
    try:
        return float(str(raw).strip().lstrip("$"))
    except (TypeError, ValueError):
        return None


def _build_openalex_query_params(query: str, page: int, per_page: int) -> Dict[str, Any]:
    """Build OpenAlex query params: exact search phrase in filter, topic perspective in search."""
    topic_query = query or ""
    if PREFIX_STRIP_REGEX:
        topic_query = re.sub(PREFIX_STRIP_REGEX, "", topic_query, flags=re.IGNORECASE)
    topic_query = topic_query.strip()
    params: Dict[str, Any] = {
        "filter": OPENALEX_BASE_FILTER,
        "per_page": per_page,
        "page": page,
    }
    if topic_query:
        params["search"] = topic_query
    return params


def _parse_openalex_work(
    work: Any,
    existing_titles: Set[str],
    existing_dois: Set[str],
) -> Optional[Tuple[Dict[str, Any], str, str]]:
    """Null-safely parse a single OpenAlex work, run academic filter and dedup without mutating input sets."""
    if not isinstance(work, dict):
        return None

    raw_title = work.get("display_name") or work.get("title") or ""
    title = (raw_title if isinstance(raw_title, str) else str(raw_title)).strip()
    if not title:
        return None

    raw_abs_idx = work.get("abstract_inverted_index")
    abstract = reconstruct_openalex_abstract(raw_abs_idx) if isinstance(raw_abs_idx, dict) else ""

    if not is_strictly_academic(work, abstract):
        return None

    raw_doi = work.get("doi") or ""
    doi_str = raw_doi if isinstance(raw_doi, str) else str(raw_doi)
    doi = (
        doi_str.replace("https://doi.org/", "")
        .replace("http://doi.org/", "")
        .strip()
        .lower()
    )
    norm_title = normalize_text(title)
    text_full = normalize_text(title + " " + abstract)

    if norm_title in existing_titles or (doi and doi in existing_dois):
        return None

    if SEARCH_PHRASES_NORM and not any(kw in text_full for kw in SEARCH_PHRASES_NORM):
        return None

    authors = []
    for authorship in (work.get("authorships") or []):
        if not isinstance(authorship, dict):
            continue
        author_obj = authorship.get("author")
        if isinstance(author_obj, dict):
            author_name = author_obj.get("display_name") or ""
            if isinstance(author_name, str) and author_name.strip():
                authors.append({"name": author_name.strip()})

    primary_loc = work.get("primary_location")
    if not isinstance(primary_loc, dict):
        primary_loc = {}
    source = primary_loc.get("source")
    if not isinstance(source, dict):
        source = {}

    raw_venue = source.get("display_name") or ""
    raw_publisher = source.get("host_organization_name") or ""
    raw_work_type = work.get("type") or ""
    raw_source_type = source.get("type") or ""
    year_val = work.get("publication_year")

    biblio = work.get("biblio")
    if not isinstance(biblio, dict):
        biblio = {}
    vol = str(biblio.get("volume") or "").strip()
    iss = str(biblio.get("issue") or "").strip()
    fp = str(biblio.get("first_page") or "").strip()
    lp = str(biblio.get("last_page") or "").strip()
    pages = f"{fp}-{lp}" if (fp and lp and fp != lp) else (fp or lp)

    paper_data = {
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "venue": raw_venue if isinstance(raw_venue, str) else str(raw_venue),
        "publisher": raw_publisher if isinstance(raw_publisher, str) else str(raw_publisher),
        "volume": vol,
        "issue": iss,
        "pages": pages,
        "workType": (raw_work_type if isinstance(raw_work_type, str) else str(raw_work_type)).lower(),
        "sourceType": (raw_source_type if isinstance(raw_source_type, str) else str(raw_source_type)).lower(),
        "year": year_val if year_val is not None else "",
        "externalIds": {"DOI": doi},
        "url": work.get("doi") or work.get("id") or "",
        "citationCount": work.get("cited_by_count") or 0,
    }
    return paper_data, norm_title, doi


def search_openalex_single_query(
    query: str,
    limit: int = 50,
    max_retries: int = 3,
    existing_titles: Set[str] = None,
    existing_dois: Set[str] = None,
) -> List[dict]:
    if existing_titles is None:
        existing_titles = set()
    if existing_dois is None:
        existing_dois = set()

    url = "https://api.openalex.org/works"
    valid_papers: List[dict] = []
    page = 1
    per_page = 200
    max_pages = 5
    max_rate_retries = 5

    print(f"🔍 [OpenAlex] Querying '{query}' (target: {limit} new papers)...")

    while len(valid_papers) < limit and page <= max_pages:
        params = _build_openalex_query_params(query, page=page, per_page=per_page)

        payload: Optional[dict] = None
        net_attempts = 0
        rate_attempts = 0
        page_error: Optional[str] = None

        while True:
            try:
                time.sleep(0.6)
                response = openalex_session.get(url, params=params, timeout=20)

                if response.status_code == 200:
                    payload = response.json()
                    break

                elif response.status_code == 429:
                    retry_after_raw = str(response.headers.get("Retry-After", "")).strip()
                    retry_after_sec = int(retry_after_raw) if retry_after_raw.isdigit() else None
                    rem_usd_raw = str(response.headers.get("X-RateLimit-Remaining-USD", "")).strip()
                    rem_usd = _parse_float_header(response.headers, "X-RateLimit-Remaining-USD")
                    body_lower = (response.text or "").lower()

                    is_daily_exhausted = (
                        "insufficient budget" in body_lower
                        or (rem_usd is not None and rem_usd < 0.005)
                        or (retry_after_sec is not None and retry_after_sec > 120)
                    )
                    if is_daily_exhausted:
                        raise OpenAlexQuotaExceededError(
                            f"OpenAlex daily quota exhausted (Remaining=${rem_usd_raw or '?'}, Retry-After={retry_after_raw or '?'}s)",
                            partial_papers=valid_papers,
                        )

                    rate_attempts += 1
                    if rate_attempts <= max_rate_retries:
                        base_wait = (retry_after_sec + 1) if retry_after_sec is not None else (rate_attempts * 4)
                        wait_sec = min(max(base_wait, rate_attempts * 3), 60)
                        print(
                            f"⏳ [OpenAlex 429 Pacing] Retry-After={retry_after_raw or '?'}s, "
                            f"Remaining=${rem_usd_raw or '?'} — waiting {wait_sec}s before retry "
                            f"({rate_attempts}/{max_rate_retries})"
                        )
                        time.sleep(wait_sec)
                        continue
                    else:
                        raise OpenAlexQuotaExceededError(
                            f"OpenAlex persistent 429 rate limit after {max_rate_retries} retries (Remaining=${rem_usd_raw or '?'})",
                            partial_papers=valid_papers,
                        )

                else:
                    msg = f"HTTP {response.status_code}"
                    print(
                        f"⚠️ [OpenAlex {msg}] Query '{query}' page {page} failed: "
                        f"{(response.text or '')[:160]}"
                    )
                    page_error = msg
                    break

            except OpenAlexQuotaExceededError:
                raise
            except Exception as e:
                net_attempts += 1
                print(
                    f"⚠️ [OpenAlex Network Error] Query '{query}' page {page} "
                    f"attempt {net_attempts}/{max_retries} failed: {e}"
                )
                if net_attempts < max_retries:
                    time.sleep(2 * net_attempts)
                else:
                    page_error = f"Network error: {e}"
                    break

        if page_error is not None:
            if valid_papers:
                print(
                    f"⚠️ Page {page} failed ({page_error}); "
                    f"preserving {len(valid_papers)} collected candidate(s) and finishing current query."
                )
                return valid_papers
            raise OpenAlexRequestError(page_error)

        results = (payload or {}).get("results") or []
        total_hits = ((payload or {}).get("meta") or {}).get("count")

        if not results:
            if page == 1:
                print(f"📭 No matching OpenAlex works for query '{query}'.")
            break

        for work in results:
            try:
                parsed = _parse_openalex_work(work, existing_titles, existing_dois)
            except Exception as parse_err:
                work_id = work.get("id", "unknown") if isinstance(work, dict) else "non-dict"
                print(f"⚠️ Skipping malformed OpenAlex work ({work_id}): {parse_err}")
                continue

            if not parsed:
                continue

            paper_data, norm_title, doi = parsed
            valid_papers.append(paper_data)
            existing_titles.add(norm_title)
            if doi:
                existing_dois.add(doi)

            if len(valid_papers) >= limit:
                break

        hits_info = f"total_hits={total_hits}, " if total_hits is not None else ""
        print(
            f"📄 OpenAlex page {page} scanned ({hits_info}page_items={len(results)}) | "
            f"Selected candidates: {len(valid_papers)}/{limit}"
        )

        if len(valid_papers) >= limit or len(results) < per_page or (
            total_hits is not None and page * per_page >= total_hits
        ):
            break

        page += 1

    return valid_papers


def search_openalex_by_snowballing(
    seed_doi: str,
    limit: int = 10,
    existing_titles: Set[str] = None,
    existing_dois: Set[str] = None,
) -> List[dict]:
    global _OPENALEX_QUOTA_EXCEEDED
    if existing_titles is None:
        existing_titles = set()
    if existing_dois is None:
        existing_dois = set()
    if not seed_doi:
        return []

    print(f"🔗 [Citation Snowball] Tracing citing papers from seed DOI [{seed_doi}]...")
    lookup_url = f"https://api.openalex.org/works/https://doi.org/{seed_doi}"

    work_id = None
    for attempt in range(2):
        try:
            resp = openalex_session.get(lookup_url, timeout=15)
            if resp.status_code == 429:
                retry_after_raw = str(resp.headers.get("Retry-After", "")).strip()
                retry_after_sec = int(retry_after_raw) if retry_after_raw.isdigit() else None
                rem_usd = _parse_float_header(resp.headers, "X-RateLimit-Remaining-USD")
                body_lower = (resp.text or "").lower()
                if (
                    attempt == 0
                    and "insufficient budget" not in body_lower
                    and (rem_usd is None or rem_usd >= 0.01)
                    and (retry_after_sec is None or retry_after_sec <= 60)
                ):
                    wait_sec = (retry_after_sec + 1) if retry_after_sec is not None else 4
                    print(f"⏳ [OpenAlex 429] Snowball seed lookup throttled; retrying in {wait_sec}s...")
                    time.sleep(wait_sec)
                    continue
                print("⏳ [OpenAlex 429] Snowball seed lookup hit rate/budget limit; tripping circuit breaker.")
                _OPENALEX_QUOTA_EXCEEDED = True
                return []
            if resp.status_code != 200:
                return []
            work_id = (resp.json() or {}).get("id")
            break
        except Exception:
            return []

    if not work_id:
        return []

    url = "https://api.openalex.org/works"
    per_page = min(max(limit * 2, 50), 200)
    params = {
        "filter": f"cites:{work_id},{OPENALEX_BASE_FILTER}",
        "per_page": per_page,
    }
    valid_papers: List[dict] = []

    for attempt in range(2):
        try:
            res = openalex_session.get(url, params=params, timeout=20)
            if res.status_code == 429:
                retry_after_raw = str(res.headers.get("Retry-After", "")).strip()
                retry_after_sec = int(retry_after_raw) if retry_after_raw.isdigit() else None
                rem_usd = _parse_float_header(res.headers, "X-RateLimit-Remaining-USD")
                body_lower = (res.text or "").lower()
                if (
                    attempt == 0
                    and "insufficient budget" not in body_lower
                    and (rem_usd is None or rem_usd >= 0.01)
                    and (retry_after_sec is None or retry_after_sec <= 60)
                ):
                    wait_sec = (retry_after_sec + 1) if retry_after_sec is not None else 4
                    print(f"⏳ [OpenAlex 429] Snowball citing list throttled; retrying in {wait_sec}s...")
                    time.sleep(wait_sec)
                    continue
                print("⏳ [OpenAlex 429] Snowball citing list hit rate/budget limit; tripping circuit breaker.")
                _OPENALEX_QUOTA_EXCEEDED = True
                return valid_papers

            if res.status_code == 200:
                results = (res.json() or {}).get("results") or []
                for work in results:
                    try:
                        parsed = _parse_openalex_work(work, existing_titles, existing_dois)
                    except Exception as parse_err:
                        err_id = work.get("id", "unknown") if isinstance(work, dict) else "non-dict"
                        print(f"⚠️ Skipping malformed snowball entry ({err_id}): {parse_err}")
                        continue
                    if not parsed:
                        continue
                    paper_data, norm_title, doi = parsed
                    valid_papers.append(paper_data)
                    existing_titles.add(norm_title)
                    if doi:
                        existing_dois.add(doi)
                    if len(valid_papers) >= limit:
                        break
            break
        except Exception as e:
            print(f"⚠️ Citation snowball lookup error: {e}")
            break

    return valid_papers


def search_openalex_stateful_matrix(
    limit: int = 50,
    existing_titles: Set[str] = None,
    existing_dois: Set[str] = None,
) -> List[dict]:
    global _OPENALEX_QUOTA_EXCEEDED
    _OPENALEX_QUOTA_EXCEEDED = False

    queries = generate_dynamic_queries()
    state = load_search_state()
    start_index = state.get("current_index", 0)
    dry_queries = state.get("dry_queries", {})

    total_queries = len(queries)
    all_collected_papers = []
    completed_queries = 0

    while len(all_collected_papers) < limit and completed_queries < total_queries:
        query_idx = (start_index + completed_queries) % total_queries
        q = queries[query_idx]

        if dry_queries.get(q, 0) >= 3:
            completed_queries += 1
            continue

        needed = limit - len(all_collected_papers)
        print(f"\n🎯 [Matrix Query {query_idx+1}/{total_queries}] Term: '{q}' (need {needed} more)")

        try:
            found = search_openalex_single_query(
                query=q,
                limit=needed,
                existing_titles=existing_titles,
                existing_dois=existing_dois,
            )
        except OpenAlexQuotaExceededError as e:
            _OPENALEX_QUOTA_EXCEEDED = True
            if e.partial_papers:
                all_collected_papers.extend(e.partial_papers)
            print(
                f"\n🛑 [Circuit Breaker] {e}! Halting further queries, preserving {len(all_collected_papers)} collected candidate(s), "
                f"without polluting dry_queries; cursor stays at [{query_idx+1}/{total_queries}] '{q}'."
            )
            break
        except OpenAlexRequestError as e:
            print(f"⚠️ [Skip Failed Query] '{q}' skipped due to transient API/network error without marking dry ({e}).")
            completed_queries += 1
            continue

        if not found:
            dry_queries[q] = dry_queries.get(q, 0) + 1
        else:
            dry_queries[q] = 0

        all_collected_papers.extend(found)
        completed_queries += 1

    next_index = (start_index + completed_queries) % total_queries
    save_search_state(next_index, dry_queries)

    return all_collected_papers


# ==========================================
# 10. Provider-Agnostic LLM Classification & Zotero Push
# ==========================================
def _call_llm_text(system_prompt: str, user_prompt: str, max_tokens: int = 2000) -> str:
    """Invoke either Anthropic Messages API (with prompt caching) or any OpenAI-compatible Chat Completions endpoint."""
    global _anthropic_client

    if LLM_PROVIDER == "anthropic":
        try:
            import anthropic  # Lazy import so non-Anthropic users don't need this package
        except ImportError as exc:
            raise RuntimeError(
                "Install `anthropic` (`pip install anthropic`) to use LLM_PROVIDER=anthropic."
            ) from exc

        if _anthropic_client is None:
            client_kwargs: Dict[str, Any] = {"api_key": LLM_API_KEY}
            if LLM_BASE_URL:
                client_kwargs["base_url"] = LLM_BASE_URL
            _anthropic_client = anthropic.Anthropic(**client_kwargs)

        response = _anthropic_client.messages.create(
            model=LLM_MODEL,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = ""
        for block in getattr(response, "content", []) or []:
            if hasattr(block, "text") and isinstance(block.text, str):
                raw_text += block.text
        return raw_text.strip()

    if LLM_PROVIDER in ("openai", "openai-compatible", "compatible"):
        base = LLM_BASE_URL or "https://api.openai.com/v1"
        endpoint = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if LLM_API_KEY:
            headers["Authorization"] = f"Bearer {LLM_API_KEY}"
        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        resp = llm_session.post(endpoint, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json() or {}
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise RuntimeError(f"Unexpected OpenAI-compatible response payload: {str(data)[:200]}")
        msg = choices[0].get("message") or {}
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            content = "".join(parts)
        return str(content).strip()

    raise ValueError(f"Unsupported LLM_PROVIDER: {LLM_PROVIDER!r} (expected 'openai' or 'anthropic').")


def classify_with_llm(
    title: str,
    abstract: str,
    venue: str = "",
    work_type: str = "",
    source_type: str = "",
    max_retries: int = 3,
) -> dict:
    clean_title = re.sub(r"\s+", " ", title).strip()
    clean_abstract = re.sub(r"\s+", " ", abstract).strip() if abstract else "No abstract available."
    prompt_lines = [f"Title: {clean_title}"]
    if venue and venue.strip():
        prompt_lines.append(f"Venue: {venue.strip()}")
    if work_type or source_type:
        prompt_lines.append(f"OpenAlex Type: {work_type or 'unknown'} (source: {source_type or 'unknown'})")
    prompt_lines.append(f"Abstract: {clean_abstract}")
    user_prompt = "\n".join(prompt_lines)

    for attempt in range(1, max_retries + 1):
        try:
            raw_text = _call_llm_text(SYSTEM_PROMPT, user_prompt, max_tokens=2000)

            json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            clean_json_str = json_match.group(0) if json_match else raw_text

            result = json.loads(clean_json_str)
            guarded_keys = apply_generic_language_guardrail(
                clean_abstract,
                result.get("collection_keys", []),
            )
            result["collection_keys"] = enforce_four_categories(guarded_keys)
            norm_rel = normalize_relevance(result.get("relevance") or result.get("ac_relevance") or "")
            result["relevance"] = norm_rel
            result["ac_relevance"] = norm_rel
            result["doc_type"] = normalize_doc_type(
                result.get("doc_type", ""),
                work_type=work_type,
                source_type=source_type,
            )
            return result

        except Exception as e:
            print(f"⚠️ LLM classification failed ({LLM_PROVIDER}/{LLM_MODEL}, attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                time.sleep(attempt * 2)
            else:
                return {
                    "relevance": "case_study",
                    "ac_relevance": "case_study",
                    "doc_type": normalize_doc_type("", work_type=work_type, source_type=source_type),
                    "collection_keys": list(DEFAULTS.values()),
                    "tags": [],
                    "one_sentence_summary": "Failed to extract summary due to API failure.",
                }


classify_with_claude = classify_with_llm


def prepare_zotero_item(paper_data: dict, ai_result: dict) -> dict:
    authors = paper_data.get("authors", [])
    creators = []
    seen_creators = set()
    for a in authors:
        parts = (a.get("name") or "").split(" ")
        if len(parts) > 1:
            fn = _clean_creator_first_name(" ".join(parts[:-1]))
            ln = parts[-1].strip()
            cr = {"creatorType": "author", "firstName": fn, "lastName": ln}
        else:
            cr = {"creatorType": "author", "firstName": "", "lastName": (a.get("name") or "").strip()}
        dedup_key = (cr["creatorType"], (cr.get("lastName") or "").lower(), (cr.get("firstName") or "").lower())
        if dedup_key not in seen_creators:
            seen_creators.add(dedup_key)
            creators.append(cr)

    doi = paper_data.get("externalIds", {}).get("DOI", "")
    citations = paper_data.get("citationCount", 0)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    work_type = paper_data.get("workType", "")
    source_type = paper_data.get("sourceType", "")

    # Dual-track document type & relevance normalization
    doc_type = normalize_doc_type(
        ai_result.get("doc_type", ""),
        work_type=work_type,
        source_type=source_type,
    )
    item_type = resolve_zotero_item_type(
        doc_type,
        work_type=work_type,
        source_type=source_type,
    )
    item_relevance = normalize_relevance(ai_result.get("relevance") or ai_result.get("ac_relevance") or "case_study")

    # Sanitize venue and recover missing/aggregator names via Crossref
    clean_publication = sanitize_publication_title(
        raw_pub_title=paper_data.get("venue", ""),
        doi=doi,
        item_type=item_type,
    )

    volume = str(paper_data.get("volume") or "").strip()
    issue = str(paper_data.get("issue") or "").strip()
    pages = str(paper_data.get("pages") or "").strip()
    publisher = _clean_book_publisher(paper_data.get("publisher") or "") if item_type in ("book", "bookSection", "conferencePaper") else ""

    need_cr_biblio = bool(doi) and (
        (item_type == "journalArticle" and not (volume and issue and pages))
        or item_type == "bookSection"
        or (item_type == "conferencePaper" and not pages)
    )
    if need_cr_biblio:
        cr_meta = fetch_crossref_metadata(doi)
        if not volume and cr_meta.get("volume"):
            volume = cr_meta["volume"]
        if not issue and cr_meta.get("issue"):
            issue = cr_meta["issue"]
        if not pages and cr_meta.get("pages"):
            pages = cr_meta["pages"]
        if item_type in ("bookSection", "conferencePaper") and not publisher and cr_meta.get("publisher"):
            publisher = _clean_book_publisher(cr_meta["publisher"])
        if item_type == "bookSection" and cr_meta.get("editors"):
            for ed in cr_meta["editors"]:
                ed_key = (
                    ed.get("creatorType", "editor"),
                    (ed.get("lastName") or ed.get("name") or "").lower(),
                    (ed.get("firstName") or "").lower(),
                )
                if ed_key not in seen_creators:
                    seen_creators.add(ed_key)
                    creators.append(ed)

    collections = list(ai_result.get("collection_keys", []))
    if ROOT_COLLECTION_KEY and ROOT_COLLECTION_KEY not in collections:
        collections.append(ROOT_COLLECTION_KEY)

    zotero_tags = build_canonical_tags(
        relevance=item_relevance,
        doc_type=doc_type,
        collections=collections,
        source_tag="#source/openalex",
    )

    clean_title = re.sub(r"\s+", " ", html.unescape(paper_data.get("title") or "Untitled")).strip()
    raw_year = paper_data.get("year")
    clean_date = str(raw_year).strip() if raw_year not in (None, "", "None") else ""

    venue_field_map = {
        "journalArticle": "publicationTitle",
        "bookSection": "bookTitle",
        "conferencePaper": "proceedingsTitle",
        "thesis": "university",
        "book": "publisher",
    }
    venue_field = venue_field_map.get(item_type, "publicationTitle")

    item_payload: Dict[str, Any] = {
        "itemType": item_type,
        "title": clean_title,
        "creators": creators,
        venue_field: clean_publication,  # Schema-matched Zotero venue field per native itemType
        "date": clean_date,
        "DOI": doi,
        "url": paper_data.get("url", ""),
        "abstractNote": paper_data.get("abstract", ""),
        "extra": (
            f"Relevance: {item_relevance}\n"
            f"DocType: {doc_type}\n"
            f"Citations: {citations}\n"
            f"Retrieved: {now_str}\n"
            f"AI Summary: {ai_result.get('one_sentence_summary', '')}"
        ),
        "tags": zotero_tags,
        "collections": collections,
    }

    if item_type == "journalArticle":
        if volume:
            item_payload["volume"] = volume
        if issue:
            item_payload["issue"] = issue
        if pages:
            item_payload["pages"] = pages
    elif item_type == "bookSection":
        if pages:
            item_payload["pages"] = pages
        if publisher:
            item_payload["publisher"] = publisher
    elif item_type == "conferencePaper":
        if pages:
            item_payload["pages"] = pages
        if publisher:
            item_payload["publisher"] = publisher

    return item_payload


def batch_push_to_zotero(items_list: list, chunk_size: int = 50) -> None:
    if not items_list:
        return

    total_count = len(items_list)
    print(f"🚀 Pushing {total_count} scholarly item(s) to Zotero...")

    for i in range(0, total_count, chunk_size):
        chunk = items_list[i : i + chunk_size]
        batch_num = (i // chunk_size) + 1
        try:
            response = zot.create_items(chunk)
            if response.get("success"):
                success_keys = list(response["success"].values())
                print(f"🎉 Chunk {batch_num} pushed to Zotero! Item Keys: {success_keys}")

                log_entry = {
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "count": len(success_keys),
                    "keys": success_keys,
                }
                history = []
                if os.path.exists(HISTORY_FILE):
                    try:
                        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                            history = json.load(f)
                    except Exception:
                        history = []
                history.append(log_entry)
                atomic_json_write(HISTORY_FILE, history)
                print("📝 Recorded batch Item Keys safely to batch_history.json.")

            else:
                print(f"❌ Chunk {batch_num} Zotero push failed: {response.get('failed')}")
        except Exception as e:
            print(f"❌ Chunk {batch_num} Zotero network error: {e}")
        time.sleep(1)
    print()


# ==========================================
# 11. Main Entrypoint
# ==========================================
def main():
    if not ROOT_COLLECTION_KEY:
        raise SystemExit("❌ ZOTERO_ROOT_COLLECTION_KEY is not configured in .env.")
    if OPENALEX_API_KEY:
        print("🔑 OpenAlex API Key loaded ($1.00/day dedicated quota mode)")
    else:
        print("⚠️ OPENALEX_API_KEY not configured; using shared anonymous IP quota mode")
    print(f"🤖 LLM Provider: {LLM_PROVIDER} | Model: {LLM_MODEL}")
    existing_titles, existing_dois = get_existing_zotero_cache(ROOT_COLLECTION_KEY)
    rejected_titles, rejected_dois = load_rejected_cache()
    seen_titles = existing_titles | rejected_titles
    seen_dois = existing_dois | rejected_dois
    if rejected_titles or rejected_dois:
        print(f"🛡️ Loaded rejection cache {REJECTED_CACHE_FILE} ({len(rejected_titles)} rejected/excluded titles will be skipped).")

    TARGET_LIMIT = 50

    papers = search_openalex_stateful_matrix(
        limit=TARGET_LIMIT,
        existing_titles=seen_titles,
        existing_dois=seen_dois,
    )

    if len(papers) < TARGET_LIMIT and not _OPENALEX_QUOTA_EXCEEDED:
        needed = TARGET_LIMIT - len(papers)
        print(f"\n💡 [Fallback Snowball] Matrix scan collected {len(papers)} item(s); running citation snowballing for remaining {needed}...")
        seed_dois = list(existing_dois)[:5]
        for s_doi in seed_dois:
            if len(papers) >= TARGET_LIMIT or _OPENALEX_QUOTA_EXCEEDED:
                break
            snowball_found = search_openalex_by_snowballing(
                seed_doi=s_doi,
                limit=TARGET_LIMIT - len(papers),
                existing_titles=seen_titles,
                existing_dois=seen_dois,
            )
            papers.extend(snowball_found)
    elif len(papers) < TARGET_LIMIT and _OPENALEX_QUOTA_EXCEEDED:
        print(f"\n🛑 [Circuit Breaker] OpenAlex daily quota exhausted; skipping snowballing and proceeding with {len(papers)} collected paper(s).")

    if not papers:
        print(f"⚠️ No new unharvested {TOPIC_NAME} papers found in OpenAlex.")
        return

    print(f"\n✅ Retrieval complete! Collected {len(papers)} candidate paper(s); starting LLM classification & gatekeeping...\n")

    items_to_push = []
    new_titles_added = []
    new_dois_added = []
    rejected_records = []
    core_count = 0
    case_count = 0

    for idx, paper in enumerate(papers, 1):
        title = paper.get("title")
        abstract = paper.get("abstract")
        venue = paper.get("venue", "")
        work_type = paper.get("workType", "")
        source_type = paper.get("sourceType", "")
        citations = paper.get("citationCount", 0)
        doi = paper.get("externalIds", {}).get("DOI", "")

        print(f"--- Processing candidate [{idx}/{len(papers)}] ---")
        print(f"📖 Title: {title}")
        print(f"📊 Citations: {citations} | OpenAlex type: {work_type or 'unknown'}")

        ai_result = classify_with_llm(
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
            print(f"🚫 [Gatekeeper] Classified as passing_mention; skipping Zotero and recording in {REJECTED_CACHE_FILE}")
            rejected_records.append({
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "title": title,
                "doi": doi,
                "reason": "passing_mention",
                "summary": ai_result.get("one_sentence_summary", ""),
            })
            print("-" * 40)
            time.sleep(0.3)
            continue

        if ac_rel == "core":
            core_count += 1
        else:
            case_count += 1

        print(f"📂 Assigned Collection Keys: {ai_result.get('collection_keys')}")
        print(f"🏷️ Tags: {ai_result.get('tags')}")
        print(f"📝 English Summary: {ai_result.get('one_sentence_summary')}")

        item = prepare_zotero_item(paper, ai_result)
        items_to_push.append(item)

        new_titles_added.append(title)
        if doi:
            new_dois_added.append(doi)

        print("-" * 40)
        time.sleep(0.3)

    print(
        f"\n📊 [Batch Summary] Core (rel:core)={core_count} | "
        f"Case study (rel:case-study)={case_count} | "
        f"Rejected (passing_mention)={len(rejected_records)}\n"
    )

    if items_to_push:
        batch_push_to_zotero(items_to_push, chunk_size=50)
        latest_version = None
        try:
            latest_version = zot.last_modified_version()
        except Exception:
            pass
        save_to_local_cache(new_titles_added, new_dois_added, cloud_version=latest_version)
    else:
        print("⚠️ No candidates passed the relevance gatekeeper in this run; nothing pushed to Zotero.")

    if rejected_records:
        save_rejected_cache(rejected_records)
        print(f"🛡️ Persisted {len(rejected_records)} rejected record(s) to {REJECTED_CACHE_FILE}.")


if __name__ == "__main__":
    main()