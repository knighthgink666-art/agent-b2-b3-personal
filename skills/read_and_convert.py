from __future__ import annotations

from skills.file_reader import file_reader
from skills.format_converter import format_converter


def read_and_convert(
    path: str,
    target_format: str,
    max_chars: int = 2000,
    output_filename: str | None = None,
    *,
    data_root: str | None = None,
    output_dir: str | None = None,
) -> dict:
    # advanced3: compose file reading and format conversion into one fixed workflow.
    read_output = file_reader(path, max_chars, data_root=data_root)
    convert_output = format_converter(
        read_output["content"],
        target_format,
        output_filename,
        output_dir,
    )
    return {
        "source": read_output["source"],
        "source_num_chars": read_output["num_chars"],
        "source_truncated": read_output["truncated"],
        "target_format": target_format,
        "formatted_text": convert_output["formatted_text"],
        "generated_file_path": convert_output["generated_file_path"],
    }
