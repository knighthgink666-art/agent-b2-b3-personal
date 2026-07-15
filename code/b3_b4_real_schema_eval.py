from __future__ import annotations

import argparse
import json
from pathlib import Path

from b3_tool_layer import execute_tool_calls, get_tools_schema
from b4_local_agent_llm import generate_ai_message
from common.io_utils import ensure_dir, read_json, read_text, write_json, write_jsonl
from common.path_utils import resolve_cli_path


SCHEMA_VARIANTS = ("auto", "minimal", "detailed")


def _load_cases(path: Path) -> list[dict]:
    """Load and validate the expected tool-call cases used by Advanced 5."""
    payload = read_json(path)
    cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases must be a non-empty array or an object containing cases")
    seen_ids = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("each evaluation case must be an object")
        case_id = case.get("case_id")
        user_input = case.get("user_input")
        expected_tool_names = case.get("expected_tool_names")
        expected_args = case.get("expected_args")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("each case needs a non-empty case_id")
        if case_id in seen_ids:
            raise ValueError(f"duplicate case_id: {case_id}")
        seen_ids.add(case_id)
        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError(f"case {case_id} needs a non-empty user_input")
        if not isinstance(expected_tool_names, list) or not all(isinstance(name, str) for name in expected_tool_names):
            raise ValueError(f"case {case_id} expected_tool_names must be a string list")
        if not isinstance(expected_args, dict):
            raise ValueError(f"case {case_id} expected_args must be an object")
    return cases


def _expected_args_match(tool_calls: list[dict], case: dict) -> bool:
    """Check only the declared key arguments, so optional extra arguments stay allowed."""
    expected_names = case["expected_tool_names"]
    if [call.get("name") for call in tool_calls] != expected_names:
        return False
    if not tool_calls:
        return False
    generated_args = tool_calls[0].get("args")
    if not isinstance(generated_args, dict):
        return False
    return all(generated_args.get(key) == value for key, value in case["expected_args"].items())


def _tool_message_statuses(tool_messages: list[dict]) -> list[str]:
    """Read B3 result statuses without changing the ToolMessage interface."""
    return [message.get("status", "error") for message in tool_messages if isinstance(message, dict)]


def _summarize(records: list[dict]) -> dict:
    total = len(records)

    def rate(predicate) -> float:
        return round(sum(1 for record in records if predicate(record)) / total, 4) if total else 0.0

    return {
        "case_count": total,
        "b4_parse_success_rate": rate(lambda record: record["b4_status"] == "success"),
        "tool_name_exact_rate": rate(lambda record: record["tool_name_exact"]),
        "expected_args_subset_match_rate": rate(lambda record: record["expected_args_subset_match"]),
        "b3_execution_success_rate": rate(lambda record: record["b3_all_success"]),
        "end_to_end_tool_call_accuracy_rate": rate(lambda record: record["end_to_end_correct"]),
    }


def evaluate_real_model_schema_variants(
    tools_config: str,
    model_config: str,
    cases_path: str,
    toolset: str,
    model_id: str,
    system_prompt_path: str,
    outdir: str,
) -> dict:
    """Advanced 5: compare real B4 prompt_json tool calls under three Schema descriptions."""
    output_dir = ensure_dir(outdir)
    cases_file = resolve_cli_path(cases_path)
    system_prompt_file = resolve_cli_path(system_prompt_path)
    cases = _load_cases(cases_file)
    system_prompt = read_text(system_prompt_file).strip()
    tools_config_path = resolve_cli_path(tools_config)
    model_config_path = resolve_cli_path(model_config)

    # Advanced 5: 将所有方案导出到同一根目录，同时保留 auto -> tools_schema.json 的原接口。
    schemas = {
        variant: get_tools_schema(str(tools_config_path), toolset, str(output_dir), schema_variant=variant)
        for variant in SCHEMA_VARIANTS
    }
    write_json({"cases": cases}, output_dir / "b4_real_eval_cases.json")

    variants = {}
    all_records = []
    for variant, schema in schemas.items():
        variant_dir = ensure_dir(output_dir / variant)
        records = []
        for case in cases:
            case_id = case["case_id"]
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": case["user_input"]},
            ]
            # Advanced 5: 仅使用真实模型生成；提示词注入使不同 Schema 描述的差异保持可见。
            llm_result = generate_ai_message(
                str(model_config_path),
                messages,
                schema,
                mode="prompt_json",
                artifact_dir=str(variant_dir / "llm_calls"),
                artifact_stem=case_id,
                model_id=model_id,
                use_native_tools_schema=False,
            )
            ai_message = llm_result["ai_message"]
            tool_calls = ai_message.get("tool_calls", []) if isinstance(ai_message, dict) else []
            generated_names = [call.get("name") for call in tool_calls if isinstance(call, dict)]
            tool_name_exact = generated_names == case["expected_tool_names"]
            expected_args_subset_match = _expected_args_match(tool_calls, case)

            # Advanced 5: B3 在不使用缓存的情况下执行真实 B4 调用，以隔离 Schema 与缓存的影响。
            tool_messages = []
            if llm_result.get("status") == "success" and tool_calls:
                tool_messages = execute_tool_calls(
                    tool_calls,
                    str(tools_config_path),
                    toolset,
                    str(variant_dir / "b3_execution" / case_id),
                    use_cache=False,
                )
            statuses = _tool_message_statuses(tool_messages)
            b3_all_success = bool(statuses) and all(status == "success" for status in statuses)
            record = {
                "case_id": case_id,
                "schema_variant": variant,
                "user_input": case["user_input"],
                "expected_tool_names": case["expected_tool_names"],
                "expected_args": case["expected_args"],
                "b4_status": llm_result.get("status"),
                "b4_error": llm_result.get("error"),
                "selected_model_id": llm_result.get("selected_model_id"),
                "generated_ai_message": ai_message,
                "generated_tool_names": generated_names,
                "tool_name_exact": tool_name_exact,
                "expected_args_subset_match": expected_args_subset_match,
                "b3_tool_statuses": statuses,
                "b3_all_success": b3_all_success,
                "end_to_end_correct": (
                    llm_result.get("status") == "success"
                    and tool_name_exact
                    and expected_args_subset_match
                    and b3_all_success
                ),
            }
            records.append(record)
            all_records.append(record)
        write_jsonl(records, variant_dir / "real_model_eval_records.jsonl")
        variants[variant] = {
            "schema_file": str(output_dir / ("tools_schema.json" if variant == "auto" else f"tools_schema_{variant}.json")),
            "records_path": str(variant_dir / "real_model_eval_records.jsonl"),
            "metrics": _summarize(records),
        }

    report = {
        "status": "success",
        "mode": "b3_advanced5_real_model_schema_eval",
        "protocol": {
            "llm_mode": "prompt_json",
            "model_id": model_id,
            "tool_schema_delivery": "prompt_injection",
            "b3_cache_enabled": False,
            "fairness_note": "Every variant receives the same cases, model id, system prompt, tools_config, and expected-answer rules.",
        },
        "cases_path": str(output_dir / "b4_real_eval_cases.json"),
        "variant_order": list(SCHEMA_VARIANTS),
        "variants": variants,
        "all_records_path": str(output_dir / "real_model_schema_eval_records.jsonl"),
        "interpretation": [
            "auto, minimal, and detailed preserve the same parameter set; only description detail differs.",
            "The primary comparison metric is end_to_end_tool_call_accuracy_rate.",
            "B4 raw outputs and parsed AIMessage artifacts are saved below each variant/llm_calls directory.",
        ],
    }
    write_jsonl(all_records, output_dir / "real_model_schema_eval_records.jsonl")
    write_json(report, output_dir / "real_model_schema_eval_report.json")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate B4 prompt_json tool-call accuracy across B3 Schema variants.")
    parser.add_argument("--tools_config", required=True)
    parser.add_argument("--model_config", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--toolset", default="basic_tools")
    parser.add_argument("--model_id", default="qwen_tool")
    parser.add_argument("--system_prompt", default="../prompts/local_tool_agent.txt")
    parser.add_argument("--outdir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = evaluate_real_model_schema_variants(
            args.tools_config,
            args.model_config,
            args.cases,
            args.toolset,
            args.model_id,
            args.system_prompt,
            args.outdir,
        )
        print(report["all_records_path"])
        print(resolve_cli_path(args.outdir) / "real_model_schema_eval_report.json")
        return 0
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
