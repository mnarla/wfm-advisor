import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Tuple

import requests
from rapidfuzz import fuzz, process, utils

from ingest.slug_utils import parse_item, parse_slug

logger = logging.getLogger(__name__)

WFM_V2_ITEMS_URL = "https://api.warframe.market/v2/items"
WFM_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "wfm-sell-timing-advisor/1.0",
    "Language": "en",
}

# User-input aliases for component keywords
COMPONENT_ALIASES: Dict[str, str] = {
    # Frame components
    "set": "set",
    "bp": "blueprint",
    "blueprint": "blueprint",
    "neuroptics": "neuroptics",
    "neuroptic": "neuroptics",
    "neuro": "neuroptics",
    "neur": "neuroptics",
    "chassis": "chassis",
    "chass": "chassis",
    "chas": "chassis",
    "systems": "systems",
    "system": "systems",
    "sys": "systems",
    # Weapon components
    "barrel": "barrel",
    "bar": "barrel",
    "receiver": "receiver",
    "rec": "receiver",
    "reciever": "receiver",
    "stock": "stock",
    "stk": "stock",
    "grip": "grip",
    "string": "string",
    "str": "string",
    "upper limb": "upper_limb",
    "upper_limb": "upper_limb",
    "upper": "upper_limb",
    "lower limb": "lower_limb",
    "lower_limb": "lower_limb",
    "lower": "lower_limb",
    "blade": "blade",
    "bld": "blade",
    "blde": "blade",
    "blades": "blade",
    "handle": "handle",
    "hnd": "handle",
    "handl": "handle",
    "disc": "disc",
    "disk": "disc",
    "guard": "guard",
    "hilt": "hilt",
    "head": "head",
    "gauntlet": "gauntlet",
    "link": "link",
    "pouch": "pouch",
    "stars": "stars",
    "star": "stars",
    "chain": "chain",
    "carapace": "carapace",
    "cerebrum": "cerebrum",
    "ornament": "ornament",
    "boot": "boot",
}

TOKEN_EXPANSIONS: Dict[str, str] = {
    "p": "prime",
    "pr": "prime",
    "excal": "excalibur",
    "valk": "valkyr",
    "trin": "trinity",
    "hyd": "hydroid",
    "sev": "sevagoth",
    "rev": "revenant",
    "ober": "oberon",
    "octa": "octavia",
    "tita": "titania",
    "vaub": "vauban",
    "voru": "voruna",
    "baru": "baruuk",
    "cali": "caliban",
    "garu": "garuda",
    "equi": "equinox",
}


@dataclass
class ResolvedQuery:
    status: Literal["resolved", "ambiguous", "not_found"]
    frame_name: Optional[str] = None
    component: Optional[str] = None  # None = whole set
    slugs: List[str] = field(default_factory=list)
    candidates: List[str] = field(default_factory=list)


# In-memory cache for live WFM catalog
_CATALOG_CACHE: Optional[Dict[str, Dict[str, str]]] = None


def fetch_prime_catalog(timeout: float = 10.0) -> Dict[str, Dict[str, str]]:
    """
    Pulls live WFM v2 items and builds a mapping of:
    item_name -> {component_type: slug}

    Filters to Prime warframe and weapon component items only.
    Builds the unique list dynamically from live data.
    """
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE

    catalog: Dict[str, Dict[str, str]] = {}
    try:
        response = requests.get(WFM_V2_ITEMS_URL, headers=WFM_HEADERS, timeout=timeout)
        response.raise_for_status()
        data = response.json().get("data", [])

        for item in data:
            tags = item.get("tags", [])
            # Filter for warframe and weapon items
            if "warframe" not in tags and "weapon" not in tags:
                continue

            item_name, component_type = parse_item(item)
            if item_name and component_type:
                if item_name not in catalog:
                    catalog[item_name] = {}
                slug = item.get("slug") or item.get("url_slug")
                if slug:
                    catalog[item_name][component_type] = slug
    except Exception as e:
        logger.warning(f"Failed to fetch live WFM catalog: {e}. Using fallback structure.")

    # Gracefully ensure Excalibur Prime exists in catalog mapping
    if "Excalibur Prime" not in catalog:
        catalog["Excalibur Prime"] = {
            "set": "excalibur_prime_set",
            "blueprint": "excalibur_prime_blueprint",
            "neuroptics": "excalibur_prime_neuroptics_blueprint",
            "chassis": "excalibur_prime_chassis_blueprint",
            "systems": "excalibur_prime_systems_blueprint",
        }

    _CATALOG_CACHE = catalog
    return _CATALOG_CACHE


def extract_component(query: str) -> Tuple[str, Optional[str]]:
    """
    Checks if user input ends with a known component keyword/alias.
    Handles compound multi-word components (e.g. 'upper limb', 'lower limb')
    and compound blueprint suffixes (e.g. 'systems bp', 'upper limb blueprint').
    Returns (remaining_query_text, component_type_or_None).
    """
    if not query or not query.strip():
        return "", None

    words = re.findall(r"[A-Za-z0-9]+", query.lower())
    if not words:
        return "", None

    # Check 3-word combinations (e.g. 'upper limb bp', 'lower limb blueprint')
    if len(words) >= 3 and words[-1] in ("blueprint", "bp"):
        two_word = f"{words[-3]} {words[-2]}"
        if two_word in COMPONENT_ALIASES:
            return " ".join(words[:-3]), COMPONENT_ALIASES[two_word]

    # Check 2-word combinations
    if len(words) >= 2:
        # e.g. 'systems bp', 'neuroptics blueprint'
        if words[-1] in ("blueprint", "bp"):
            second_last = words[-2]
            if second_last in COMPONENT_ALIASES and second_last not in ("bp", "blueprint", "set"):
                return " ".join(words[:-2]), COMPONENT_ALIASES[second_last]
        # e.g. 'upper limb', 'lower limb'
        two_word = f"{words[-2]} {words[-1]}"
        if two_word in COMPONENT_ALIASES:
            return " ".join(words[:-2]), COMPONENT_ALIASES[two_word]

    # Check last word (e.g. 'barrel', 'sys', 'bp', 'receiver')
    last_word = words[-1]
    if last_word in COMPONENT_ALIASES:
        return " ".join(words[:-1]), COMPONENT_ALIASES[last_word]

    return " ".join(words), None


def normalize_query_for_matching(query: str) -> str:
    """
    Normalizes token abbreviations (e.g. 'p' -> 'prime', 'excal' -> 'excalibur').
    Appends 'prime' if missing to match against full Prime names.
    """
    tokens = re.findall(r"[A-Za-z0-9]+", query.lower())
    if not tokens:
        return ""
    expanded = [TOKEN_EXPANSIONS.get(t, t) for t in tokens]
    if "prime" not in expanded:
        expanded.append("prime")
    return " ".join(expanded)


def _build_slugs_for_item(
    item_name: str,
    component: Optional[str],
    catalog: Dict[str, Dict[str, str]],
) -> List[str]:
    """
    Constructs the list of slugs for a resolved item and component.
    Fetches real component slugs directly from the live catalog.
    """
    item_comps = catalog.get(item_name, {})
    base_slug = item_name.lower().replace(" ", "_")

    if component:
        # Single component requested
        if component in item_comps:
            return [item_comps[component]]
        # Fallback if component is blade/blades alias
        if component == "blade" and "blades" in item_comps:
            return [item_comps["blades"]]
        # Fallback slug synthesis
        if component in ("neuroptics", "chassis", "systems"):
            return [f"{base_slug}_{component}_blueprint"]
        return [f"{base_slug}_{component}"]

    # When no specific component is specified (e.g. "gauss prime", "soma prime"),
    # return the Set item slug directly.
    if item_comps:
        if "set" in item_comps:
            return [item_comps["set"]]
        return [list(item_comps.values())[0]]

    # Fallback if item has no entries in catalog
    return [f"{base_slug}_set"]


def resolve_item_query(
    user_input: str,
    catalog: Optional[Dict[str, Dict[str, str]]] = None,
) -> ResolvedQuery:
    """
    Resolves free-text user input into WFM item slug(s).

    Scoring rules:
      - score >= 85: auto-resolve to top match (status='resolved')
      - 65 <= score < 85: return top 2-3 candidates (status='ambiguous')
      - score < 65: return status='not_found'

    Conservative-default principle: never raises exceptions on malformed input.
    """
    try:
        if not user_input or not isinstance(user_input, str) or not user_input.strip():
            return ResolvedQuery(status="not_found")

        current_catalog = catalog if catalog is not None else fetch_prime_catalog()
        item_names = list(current_catalog.keys())
        if not item_names:
            return ResolvedQuery(status="not_found")

        # 1. Parse component keyword
        remaining_text, component = extract_component(user_input)
        if not remaining_text:
            return ResolvedQuery(status="not_found", component=component)

        # 2. Normalize remaining query for matching
        normalized_query = normalize_query_for_matching(remaining_text)
        if not normalized_query:
            return ResolvedQuery(status="not_found", component=component)

        # 3. Fuzzy match against item name list using rapidfuzz token_sort_ratio
        results = process.extract(
            normalized_query,
            item_names,
            scorer=fuzz.token_sort_ratio,
            processor=utils.default_process,
            limit=5,
        )

        if not results:
            return ResolvedQuery(status="not_found", component=component)

        top_match, top_score, _ = results[0]

        # Score >= 85 -> auto-resolve
        if top_score >= 85.0:
            slugs = _build_slugs_for_item(top_match, component, current_catalog)
            return ResolvedQuery(
                status="resolved",
                frame_name=top_match,
                component=component,
                slugs=slugs,
            )

        # 65 <= Score < 85 -> ambiguous
        ambiguous_candidates = [
            match for match, score, _ in results if 65.0 <= score < 85.0
        ]
        if ambiguous_candidates:
            return ResolvedQuery(
                status="ambiguous",
                component=component,
                candidates=ambiguous_candidates[:3],
            )

        # Score < 65 -> not found
        return ResolvedQuery(status="not_found", component=component)

    except Exception as e:
        logger.error(f"Unexpected error in resolve_item_query for input '{user_input}': {e}")
        return ResolvedQuery(status="not_found")
