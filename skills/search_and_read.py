from __future__ import annotations

from skills.file_reader import file_reader
from skills.local_file_search import local_file_search


def search_and_read(
    query: str,
    root_dir: str = "docs",
    file_types: list[str] | None = None,
    top_k: int = 5,
    read_rank: int = 1,
    max_chars: int = 2000,
    *,
    data_root: str | None = None,
) -> dict:
    # advanced3: compose local search and file reading so the best matched file can be opened directly.
    if not isinstance(read_rank, int) or isinstance(read_rank, bool) or read_rank <= 0:
        raise ValueError("read_rank must be a positive integer")
    search_output = local_file_search(query, root_dir, file_types, top_k, data_root=data_root)
    results = search_output["results"]
    if read_rank > len(results):
        raise ValueError(f"read_rank {read_rank} exceeds search result count {len(results)}")
    selected = results[read_rank - 1]
    read_output = file_reader(selected["path"], max_chars, data_root=data_root)
    return {
        "query": query,
        "selected_rank": read_rank,
        "selected_path": selected["path"],
        "selected_score": selected["score"],
        "selected_snippet": selected["snippet"],
        "search_results": results,
        "content": read_output["content"],
        "num_chars": read_output["num_chars"],
        "truncated": read_output["truncated"],
    }
