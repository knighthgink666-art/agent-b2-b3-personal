from __future__ import annotations

import json

from skills.format_converter import format_converter
from skills.table_analyzer import table_analyzer


def analyze_and_convert_table(
    path: str,
    target_format: str,
    max_rows_preview: int = 5,
    describe: bool = True,
    output_filename: str | None = None,
    *,
    data_root: str | None = None,
    output_dir: str | None = None,
) -> dict:
    # advanced3: compose table analysis and report conversion into a reusable reporting Skill.
    table_output = table_analyzer(path, max_rows_preview, describe, data_root=data_root)
    summary_text = json.dumps(table_output, ensure_ascii=False, indent=2)
    convert_output = format_converter(summary_text, target_format, output_filename, output_dir)
    return {
        "source": table_output["path"],
        "num_rows": table_output["num_rows"],
        "num_columns": table_output["num_columns"],
        "columns": table_output["columns"],
        "table_summary": table_output,
        "target_format": target_format,
        "formatted_text": convert_output["formatted_text"],
        "generated_file_path": convert_output["generated_file_path"],
    }
