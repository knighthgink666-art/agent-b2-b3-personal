from __future__ import annotations

import re

from skills import SkillError, resolve_data_path  # advanced1&4: enhanced search raises structured SkillError.


def _normalize_extensions(file_types: list[str] | None) -> set[str]:
    # advanced1: centralize extension normalization for clearer validation and future search tuning.
    extensions = file_types or ["txt", "md"]
    if not isinstance(extensions, list) or not all(isinstance(item, str) and item.strip() for item in extensions):
        raise SkillError(
            "file_types must be a list of non-empty strings",
            code="INVALID_FILE_TYPES",
            category="input",
            details={"file_types": file_types},
        )
    normalized = {f".{item.lower().lstrip('.')}" for item in extensions}
    if not normalized.issubset({".txt", ".md"}):
        raise SkillError(
            "local_file_search only supports txt and md",
            code="UNSUPPORTED_FILE_TYPE",
            category="input",
            details={"file_types": sorted(normalized)},
        )
    return normalized


def _query_terms(query: str) -> tuple[str, list[str]]:
    # advanced1: support phrase scoring while still using individual terms for recall.
    normalized_query = query.strip()
    terms = [term.casefold() for term in re.split(r"\s+", normalized_query) if term]
    unique_terms = list(dict.fromkeys(terms))
    return normalized_query.casefold(), unique_terms


def _snippet(text: str, terms: list[str], radius: int = 80) -> str:
    # advanced1: center snippets around the earliest matched term with a slightly wider context.
    lowered = text.casefold()
    positions = [lowered.find(term.casefold()) for term in terms]
    positions = [position for position in positions if position >= 0]
    start = max(0, (min(positions) if positions else 0) - radius)
    end = min(len(text), start + radius * 2)
    prefix = "..." if start else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end].replace("\n", " ").strip() + suffix


def _score_document(path_name: str, text: str, phrase: str, terms: list[str]) -> tuple[int, list[str], str]:
    # advanced1: combine phrase hits, term frequency, and filename matches for better local ranking.
    lowered_text = text.casefold()
    lowered_name = path_name.casefold()
    phrase_hits = lowered_text.count(phrase) if phrase else 0
    term_hits = {term: lowered_text.count(term) for term in terms}
    filename_hits = {term: lowered_name.count(term) for term in terms}
    matched_terms = [term for term in terms if term_hits[term] or filename_hits[term]]
    if not matched_terms:
        return 0, [], "none"
    score = phrase_hits * 10
    score += sum(term_hits.values()) * 3
    score += sum(filename_hits.values()) * 5
    score += len(matched_terms)
    match_type = "phrase" if phrase_hits else "terms"
    return score, matched_terms, match_type


def local_file_search(
    query: str,
    root_dir: str = "docs",
    file_types: list[str] | None = None,
    top_k: int = 5,
    require_all_terms: bool = False,  # advanced1: optionally require every query term to match a result.
    *,
    data_root: str | None = None,
) -> dict:
    # advanced1: enhanced local retrieval with phrase/term scoring and optional all-term filtering.
    if not isinstance(query, str) or not query.strip():
        raise SkillError("query must be a non-empty string", code="EMPTY_QUERY", category="input")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise SkillError("top_k must be a positive integer", code="INVALID_TOP_K", category="input", details={"top_k": top_k})
    if not isinstance(require_all_terms, bool):
        raise SkillError(
            "require_all_terms must be boolean",
            code="INVALID_REQUIRE_ALL_TERMS",
            category="input",
            details={"require_all_terms": require_all_terms},
        )
    search_root, data_root_path = resolve_data_path(root_dir, data_root)
    if not search_root.is_dir():
        raise SkillError(
            f"search directory not found: {root_dir}",
            code="SEARCH_ROOT_NOT_FOUND",
            category="file",
            details={"root_dir": root_dir},
        )
    normalized_extensions = _normalize_extensions(file_types)
    phrase, terms = _query_terms(query)
    results = []
    for path in sorted(search_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in normalized_extensions:
            continue
        text = path.read_text(encoding="utf-8")
        score, matched_terms, match_type = _score_document(path.name, text, phrase, terms)
        if require_all_terms and len(matched_terms) < len(terms):
            continue
        if score:
            results.append(
                {
                    "path": path.relative_to(data_root_path).as_posix(),
                    "score": score,
                    "match_type": match_type,  # advanced1: explain whether the result came from phrase or term matching.
                    "matched_terms": matched_terms,  # advanced1: expose matched terms for debugging and presentation.
                    "snippet": _snippet(text, matched_terms),
                }
            )
    results.sort(key=lambda item: (-item["score"], item["path"]))
    return {"results": results[:top_k]}
