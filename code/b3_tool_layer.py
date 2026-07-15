from __future__ import annotations

import argparse
import hashlib  # Advanced 3: 为重复工具调用构建稳定的全局缓存键。
import importlib
import inspect
import json
import shutil  # Advanced 3&4: 在重复批量测试前清理基准测试用例的输出目录。
import sys
from copy import deepcopy  # Advanced 3: 返回缓存 SkillResult 的副本，避免修改已存储的缓存条目。
from pathlib import Path
from types import UnionType
from time import perf_counter
from typing import Any, Union, get_args, get_origin, get_type_hints

from common.io_utils import append_jsonl, read_json, read_yaml, write_json
from common.logging_utils import now_iso
from common.path_utils import bootstrap_project_root, resolve_cli_path, resolve_from_file
from common.schemas import make_skill_result, make_tool_message, normalize_tool_call


bootstrap_project_root()


JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}
HIDDEN_SCHEMA_PARAMETERS = {"data_root", "output_dir"}
# Advanced 2: 这些错误属于确定性的输入/配置失败，不应重试。
NON_RETRYABLE_EXCEPTIONS = {"FileNotFoundError", "ValueError", "TypeError"}
TOOL_CALL_CACHE_PATH = Path(__file__).resolve().parents[1] / "outputs" / "B3_tool_cache" / "tool_call_cache.json"  # Advanced 3: 供 CLI 和完整系统导入共用的一份 B3 工具调用缓存。
PYTHON_JSON_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    dict: "object",
    list: "array",
}
SCHEMA_VARIANTS = {"auto", "minimal", "detailed"}  # Advanced 5: 用于对比的 Schema 设计方案，不改变 auto 的默认行为。


def _load_tools_config(tools_config: str | Path) -> tuple[Path, dict]:
    config_path = Path(tools_config).resolve()
    config = read_yaml(config_path)
    if not isinstance(config, dict):
        raise ValueError("tools.yaml must contain an object")
    if not isinstance(config.get("tools"), dict) or not isinstance(config.get("toolsets"), dict):
        raise ValueError("tools.yaml must define tools and toolsets")
    return config_path, config


def _resolve_toolset(config: dict, toolset: str | None) -> tuple[str, list[str]]:
    selected = toolset or config.get("default_toolset")
    if not isinstance(selected, str) or selected not in config["toolsets"]:
        raise ValueError(f"toolset does not exist: {selected}")
    names = config["toolsets"][selected]
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError(f"toolset {selected} must be a list of tool names")
    return selected, names


def _parameter_schema(tool: dict) -> dict:
    raw_parameters = tool.get("parameters", {})
    if not isinstance(raw_parameters, dict):
        raise ValueError("tool parameters must be an object")
    properties = {}
    for name, definition in raw_parameters.items():
        if not isinstance(definition, dict) or definition.get("type") not in JSON_TYPES:
            raise ValueError(f"invalid parameter schema for {name}")
        properties[name] = dict(definition)
    required = tool.get("required", [])
    if not isinstance(required, list) or not all(name in properties for name in required):
        raise ValueError("required parameters must reference declared properties")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _load_tool_function(tool: dict):
    # Advanced 1: 加载真实 Skill 函数，以便从 Python 代码推导 Schema。
    try:
        module = importlib.import_module(tool["module"])
        return getattr(module, tool["function"])
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(f"cannot load configured tool {tool.get('function')}: {exc}") from exc


def _json_type_from_annotation(annotation: Any) -> dict:
    # Advanced 1: 将 str、int、list[str] 等 Python 类型注解映射为 JSON Schema 类型。
    if annotation is inspect.Signature.empty or annotation is Any:
        return {"type": "string"}
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {Union, UnionType}:
        non_none = [item for item in args if item is not type(None)]
        return _json_type_from_annotation(non_none[0]) if non_none else {"type": "string"}
    if origin in {list, tuple, set}:
        schema = {"type": "array"}
        if args:
            schema["items"] = _json_type_from_annotation(args[0])
        return schema
    if origin is dict:
        return {"type": "object"}
    return {"type": PYTHON_JSON_TYPES.get(annotation, "string")}


def _auto_parameter_schema(tool: dict) -> dict:
    # Advanced 1: 直接从 Skill 函数签名推导对外参数和必填字段。
    function = _load_tool_function(tool)
    signature = inspect.signature(function)
    type_hints = get_type_hints(function)
    hidden = set(tool.get("hidden_parameters", [])) | HIDDEN_SCHEMA_PARAMETERS
    manual_parameters = tool.get("parameters", {})
    if manual_parameters is None:
        manual_parameters = {}
    if not isinstance(manual_parameters, dict):
        raise ValueError("tool parameters must be an object")
    properties = {}
    required = []
    for name, parameter in signature.parameters.items():
        if name in hidden:
            continue
        if parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            continue
        manual = manual_parameters.get(name, {})
        if manual and not isinstance(manual, dict):
            raise ValueError(f"invalid parameter schema for {name}")
        definition = _json_type_from_annotation(type_hints.get(name, parameter.annotation))
        if "description" in manual:
            definition["description"] = manual["description"]
        if "items" in manual and definition.get("type") == "array":
            definition["items"] = manual["items"]
        properties[name] = definition
        if parameter.default is inspect.Signature.empty:
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _effective_parameter_schema(tool: dict) -> dict:
    # Advanced 1: 除非工具选择函数推导 Schema，否则保留手写 Schema。
    if tool.get("auto_schema"):
        return _auto_parameter_schema(tool)
    return _parameter_schema(tool)


def _validate_schema_variant(schema_variant: str) -> str:
    # Advanced 5: 保持 auto 作为兼容 B1/B4 的默认 Schema，同时支持对比方案。
    if schema_variant not in SCHEMA_VARIANTS:
        raise ValueError(f"schema_variant must be one of: {', '.join(sorted(SCHEMA_VARIANTS))}")
    return schema_variant


def _schema_output_filename(schema_variant: str) -> str:
    # Advanced 5: 为 auto 保留原 tools_schema.json 文件名；其他方案使用明确文件名。
    return "tools_schema.json" if schema_variant == "auto" else f"tools_schema_{schema_variant}.json"


def _schema_report_filename(schema_variant: str) -> str:
    # Advanced 5: 导出对比方案时避免覆盖原 auto Schema 报告。
    return "tool_schema_report.json" if schema_variant == "auto" else f"tool_schema_report_{schema_variant}.json"


def _minimal_parameter_schema(parameter_schema: dict) -> dict:
    # Advanced 5: minimal Schema 保留所有参数，但移除冗长描述以缩短工具提示词。
    properties = {}
    required = list(parameter_schema.get("required", []))
    source_properties = parameter_schema.get("properties", {})
    for name, definition in source_properties.items():
        minimal = {"type": definition.get("type", "string")}
        if "items" in definition:
            minimal["items"] = definition["items"]
        properties[name] = minimal
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _detailed_parameter_schema(name: str, parameter_schema: dict) -> dict:
    # Advanced 5: detailed Schema 保留所有参数，并补充描述以提升模型侧清晰度。
    detailed = deepcopy(parameter_schema)
    required = set(detailed.get("required", []))
    for param_name, definition in detailed.get("properties", {}).items():
        if "description" not in definition:
            requirement = "Required" if param_name in required else "Optional"
            definition["description"] = f"{requirement} argument `{param_name}` for tool `{name}`."
    return detailed


def _schema_entry_for_variant(name: str, tool: dict, schema_variant: str) -> dict:
    # Advanced 5: 按 auto/minimal/detailed 设计规则构建单个工具 Schema 条目。
    parameter_schema = _effective_parameter_schema(tool)
    returns = tool["returns"]
    if schema_variant == "minimal":
        description = f"Use `{name}`."
        parameters = _minimal_parameter_schema(parameter_schema)
        function = {"name": name, "description": description, "parameters": parameters}
    elif schema_variant == "detailed":
        return_names = ", ".join(returns)
        description = (
            f"{tool['description']} Return fields: {return_names}. "
            "Use only declared parameters; do not invent extra arguments."
        )
        parameters = _detailed_parameter_schema(name, parameter_schema)
        function = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "x-returns": {"type": "object", "properties": returns},
            "x-schema-variant": "detailed",
        }
    else:
        function = {
            "name": name,
            "description": tool["description"],
            "parameters": parameter_schema,
            "x-returns": {"type": "object", "properties": returns},
        }
    return {"type": "function", "function": function}


def get_tools_schema(
    tools_config: str,
    toolset: str,
    outdir: str | None = None,
    schema_variant: str = "auto",  # Advanced 5: B1/B4 可选请求 minimal 或 detailed Schema；auto 仍是默认值。
) -> list[dict]:
    schema_variant = _validate_schema_variant(schema_variant)
    _, config = _load_tools_config(tools_config)
    selected, tool_names = _resolve_toolset(config, toolset)
    schema = []
    for name in tool_names:
        tool = config["tools"].get(name)
        if not isinstance(tool, dict):
            raise ValueError(f"toolset references missing tool: {name}")
        for field in ("module", "function", "description", "returns"):
            if field not in tool:
                raise ValueError(f"tool {name} missing {field}")
        returns = tool["returns"]
        if not isinstance(returns, dict):
            raise ValueError(f"tool {name} returns must be an object")
        schema.append(_schema_entry_for_variant(name, tool, schema_variant))
    if outdir:
        output_dir = Path(outdir)
        write_json(schema, output_dir / _schema_output_filename(schema_variant))
        write_json(
            {
                "status": "success",
                "toolset": selected,
                "schema_variant": schema_variant,
                "schema_file": _schema_output_filename(schema_variant),
                "tool_count": len(schema),
                "tools": tool_names,
            },
            output_dir / _schema_report_filename(schema_variant),
        )
    return schema


def _validate_args(args: dict, definition: dict) -> None:
    parameter_schema = _effective_parameter_schema(definition)
    properties = parameter_schema["properties"]
    missing = [name for name in parameter_schema["required"] if name not in args]
    if missing:
        raise ValueError(f"missing required parameters: {', '.join(missing)}")
    unknown = sorted(set(args) - set(properties))
    if unknown:
        raise ValueError(f"unknown parameters: {', '.join(unknown)}")
    for name, value in args.items():
        expected_name = properties[name]["type"]
        expected = JSON_TYPES[expected_name]
        if expected_name in {"integer", "number"} and isinstance(value, bool):
            valid = False
        else:
            valid = isinstance(value, expected)
        if not valid:
            raise ValueError(f"parameter {name} must be {expected_name}")
        if expected_name == "array" and "items" in properties[name]:
            item_type = properties[name]["items"].get("type")
            if item_type in JSON_TYPES and not all(isinstance(item, JSON_TYPES[item_type]) for item in value):
                raise ValueError(f"parameter {name} contains invalid items")


def _error_result(name: str, args: dict, exc: Exception, latency_ms: float = 0.0) -> dict:
    from skills import skill_error_payload  # advanced4: B3 封装 Skill 失败时复用 B2 的错误分类。

    return make_skill_result(
        name,
        "error",
        args,
        None,
        skill_error_payload(exc),  # advanced4: 保持 B1/B4 ToolMessage 格式，同时丰富错误内容。
        latency_ms,
    )


class _ToolRetryError(Exception):
    # Advanced 2: 在保留原始 Skill 异常的同时携带重试尝试详情。
    def __init__(self, original: Exception, attempts: list[dict]):
        super().__init__(str(original))
        self.original = original
        self.attempts = attempts


def _retry_settings(tool: dict) -> tuple[int, set[str]]:
    # Advanced 2: 工具可针对可恢复运行时失败选择有限重试。
    retry = tool.get("retry", {})
    if retry is None:
        retry = {}
    if not isinstance(retry, dict):
        raise ValueError("tool retry config must be an object")
    max_attempts = retry.get("max_attempts", 1)
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
        raise ValueError("retry.max_attempts must be a positive integer")
    retry_on = retry.get("retry_on", [])
    if not isinstance(retry_on, list) or not all(isinstance(name, str) for name in retry_on):
        raise ValueError("retry.retry_on must be a list of exception type names")
    return max_attempts, set(retry_on)


def _should_retry(exc: Exception, retry_on: set[str], attempt: int, max_attempts: int) -> bool:
    # Advanced 2: 仅对已配置的异常类别重试，且不超过最大尝试次数。
    exception_names = {cls.__name__ for cls in type(exc).mro()}
    if exception_names & NON_RETRYABLE_EXCEPTIONS:
        return False
    return attempt < max_attempts and bool(exception_names & retry_on)


def _call_tool_with_retry(function, kwargs: dict, tool: dict) -> tuple[dict, int, list[dict]]:
    # Advanced 2: 在保留最终 Skill 输出的同时，以有限次数执行重试。
    max_attempts, retry_on = _retry_settings(tool)
    attempts = []
    for attempt in range(1, max_attempts + 1):
        try:
            output = function(**kwargs)
            attempts.append({"attempt": attempt, "status": "success"})
            return output, attempt, attempts
        except Exception as exc:
            retryable = _should_retry(exc, retry_on, attempt, max_attempts)
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "retryable": retryable,
                }
            )
            if not retryable:
                raise _ToolRetryError(exc, attempts) from exc
    raise RuntimeError("retry loop exhausted unexpectedly")


def _tool_call_cache_key(name: str, args: dict) -> str:
    # Advanced 3: 对规范化工具名和 args 求哈希，使等价调用共用一个缓存条目。
    payload = json.dumps({"name": name, "args": args}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_tool_call_cache(cache_path: Path = TOOL_CALL_CACHE_PATH) -> dict:
    # Advanced 3: 延迟加载全局缓存；缓存文件缺失表示空缓存，而不是错误。
    if not cache_path.exists():
        return {}
    data = read_json(cache_path)
    if not isinstance(data, dict):
        raise ValueError(f"tool call cache must be a JSON object: {cache_path}")
    return data


def _save_tool_call_cache(cache: dict, cache_path: Path = TOOL_CALL_CACHE_PATH) -> None:
    # Advanced 3: 将成功的工具结果持久化到共享缓存文件。
    write_json(cache, cache_path)


def _clear_tool_call_cache(cache_path: Path = TOOL_CALL_CACHE_PATH) -> dict:
    # Advanced 3: CLI 清理模式只删除 B3 生成的全局缓存文件。
    existed = cache_path.exists()
    if existed:
        cache_path.unlink()
    return {"cache_path": str(cache_path), "cleared": existed}


def _cache_max_entries(config: dict) -> int:
    # Advanced 3: 从 tools.yaml 读取有界缓存大小，默认使用便于演示的小型 LRU 窗口。
    cache_settings = config.get("settings", {}).get("cache", {})
    if cache_settings is None:
        cache_settings = {}
    if not isinstance(cache_settings, dict):
        raise ValueError("settings.cache must be an object")
    max_entries = cache_settings.get("max_entries", 10)
    if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1:
        raise ValueError("settings.cache.max_entries must be a positive integer")
    return max_entries


def _path_fingerprint(path: Path, data_root: Path, kind: str = "file") -> dict:
    # Advanced 3: 存储轻量依赖元数据，使文件变化时缓存结果失效。
    resolved = path.resolve()
    exists = resolved.exists()
    stat = resolved.stat() if exists else None
    try:
        display_path = resolved.relative_to(data_root.resolve()).as_posix()
    except ValueError:
        display_path = str(resolved)
    return {
        "kind": kind,
        "path": display_path,
        "exists": exists,
        "mtime_ns": stat.st_mtime_ns if stat else None,
        "size": stat.st_size if stat else None,
    }


def _data_path_from_args(args: dict, data_root: Path, key: str = "path") -> Path | None:
    # Advanced 3: 相对 data_root 解析类文件工具参数，以便追踪依赖。
    value = args.get(key)
    if not isinstance(value, str) or not value:
        return None
    return (data_root / value).resolve()


def _cache_dependencies(name: str, args: dict, data_root: Path) -> list[dict]:
    # Advanced 3: 将每个可缓存 Skill 调用映射到可能使其结果过期的数据文件。
    if name in {"file_reader", "table_analyzer"}:
        path = _data_path_from_args(args, data_root)
        return [_path_fingerprint(path, data_root)] if path else []
    if name == "local_file_search":
        root_dir = args.get("root_dir", "docs")
        if not isinstance(root_dir, str) or not root_dir:
            return []
        search_root = (data_root / root_dir).resolve()
        file_types = args.get("file_types") or ["txt", "md"]
        if not isinstance(file_types, list) or not all(isinstance(item, str) for item in file_types):
            file_types = ["txt", "md"]
        extensions = {f".{item.lower().lstrip('.')}" for item in file_types}
        if not search_root.is_dir():  # Advanced 3&4: 缺失的搜索根目录仍需依赖标记，以便后续失效。
            return [_path_fingerprint(search_root, data_root, "directory")]
        dependencies = []  # Advanced 3&4: 通过文件列表追踪已有目录，避免目录 mtime 变化带来噪声。
        for path in sorted(search_root.rglob("*")):
            if path.is_file() and path.suffix.lower() in extensions:
                dependencies.append(_path_fingerprint(path, data_root))
        return dependencies
    return []


def _cache_entry_is_valid(entry: dict, dependencies: list[dict]) -> tuple[bool, str | None]:
    # Advanced 3: 只有记录的依赖仍然匹配时，缓存结果才可复用。
    cached_dependencies = entry.get("dependencies")
    if cached_dependencies is None:
        return False, "missing_dependencies"
    if cached_dependencies != dependencies:
        return False, "dependency_changed"
    return True, None


def _touch_cache_entry(entry: dict) -> None:
    # Advanced 3: 更新 LRU 淘汰策略使用的访问元数据。
    entry["last_accessed_at"] = now_iso()


def _prune_lru_cache(cache: dict, max_entries: int) -> int:
    # Advanced 3: 当全局缓存超过配置大小时，淘汰最久未使用的条目。
    overflow = len(cache) - max_entries
    if overflow <= 0:
        return 0
    ordered_keys = sorted(
        cache,
        key=lambda key: cache[key].get("last_accessed_at") or cache[key].get("last_hit_at") or cache[key].get("created_at") or "",
    )
    for key in ordered_keys[:overflow]:
        del cache[key]
    return overflow


def execute_tool_calls(
    tool_calls: list[dict],
    tools_config: str,
    toolset: str | None = None,
    outdir: str | None = None,
    use_cache: bool = True,  # Advanced 3: 系统导入默认启用缓存；CLI 可通过 --no_cache 禁用。
) -> list[dict]:
    config_path, config = _load_tools_config(tools_config) #读取工具配置并确定工具范围
    selected, allowed_tools = _resolve_toolset(config, toolset)
    if not isinstance(tool_calls, list):
        raise ValueError("tool_calls must be a list")
    data_root_setting = config.get("settings", {}).get("data_root", "../data")
    resolved_data_root = resolve_from_file(data_root_setting, config_path)
    tool_messages = []
    log_records = []
    output_dir = Path(outdir) if outdir else None
    cache_max_entries = _cache_max_entries(config)  # Advanced 3: 有界 LRU 缓存大小来自 tools.yaml。
    tool_call_cache = _load_tool_call_cache() if use_cache else {}  # Advanced 3: 每个执行批次只加载一次全局缓存。
    cache_dirty = False  # Advanced 3: 没有缓存条目变化时避免重写缓存文件。
    for index, raw_call in enumerate(tool_calls):  #逐条处理 Tool Call
        start = perf_counter()
        # Advanced 2: 初始化单次调用的重试元数据，以便生成清晰日志。
        attempt_count = 0
        attempts = []
        cache_enabled = False  # Advanced 3: 记录本次调用是否具备缓存资格。
        cache_hit = False  # Advanced 3: 记录是否因缓存匹配而跳过执行。
        cache_key = None  # Advanced 3: 在日志中暴露稳定缓存键，便于验证和调试。
        cache_invalidated = False  # Advanced 3: 记录过期依赖元数据是否导致重新执行。
        cache_invalidation_reason = None  # Advanced 3: 说明丢弃先前缓存条目的原因。
        cache_evicted_count = 0  # Advanced 3: 记录有界缓存大小导致的 LRU 移除数量。
        dependencies = []  # Advanced 3: 当前工具调用的依赖指纹。
        try:
            call = normalize_tool_call(raw_call, index)  #统一输入格式
        except Exception as exc:
            call = {"id": f"call_{index + 1:03d}", "name": "unknown", "args": {}}
            result = _error_result(call["name"], call["args"], exc)
        else:
            name = call["name"]
            args = call["args"]
            if name not in allowed_tools or name not in config["tools"]:  #检查工具是否允许调用
                result = _error_result(name, args, ValueError(f"tool is not available in {selected}: {name}"))
            else:
                definition = config["tools"][name]
                try:
                    _validate_args(args, definition)  #校验参数
                    cache_enabled = use_cache and definition.get("cache", True)  # Advanced 3: 工具可选择退出缓存；默认启用缓存。
                    if cache_enabled:  # Advanced 3: 在校验后、导入/运行 Skill 前查询缓存。
                        cache_key = _tool_call_cache_key(name, args)
                        dependencies = _cache_dependencies(name, args, resolved_data_root)
                        cached_entry = tool_call_cache.get(cache_key)
                        if isinstance(cached_entry, dict) and isinstance(cached_entry.get("skill_result"), dict):
                            valid_cache_entry, cache_invalidation_reason = _cache_entry_is_valid(cached_entry, dependencies)
                            if valid_cache_entry:
                                cache_hit = True
                                latency_ms = round((perf_counter() - start) * 1000, 3)
                                result = deepcopy(cached_entry["skill_result"])
                                result["latency_ms"] = latency_ms
                                cached_entry["hit_count"] = int(cached_entry.get("hit_count", 0)) + 1
                                cached_entry["last_hit_at"] = now_iso()
                                _touch_cache_entry(cached_entry)
                                cache_dirty = True
                            else:
                                cache_invalidated = True
                                del tool_call_cache[cache_key]
                                cache_dirty = True
                    if not cache_hit:
                        module = importlib.import_module(definition["module"])  #动态导入并执行 B2 Skill
                        function = getattr(module, definition["function"])
                        kwargs = dict(args)
                        signature = inspect.signature(function)
                        if "data_root" in signature.parameters:
                            kwargs["data_root"] = str(resolved_data_root)
                        if "output_dir" in signature.parameters:
                            kwargs["output_dir"] = str(output_dir) if output_dir else None
                        output, attempt_count, attempts = _call_tool_with_retry(function, kwargs, definition) #按配置重试(部分异常可以重试)
                        latency_ms = round((perf_counter() - start) * 1000, 3)
                        result = make_skill_result(name, "success", args, output, None, latency_ms)  #生成统一结果
                        if cache_enabled and cache_key:  # Advanced 3: 只缓存成功的真实执行结果。
                            accessed_at = now_iso()
                            tool_call_cache[cache_key] = {
                                "tool_name": name,
                                "args": deepcopy(args),
                                "skill_result": deepcopy(result),
                                "dependencies": deepcopy(dependencies),
                                "created_at": accessed_at,
                                "last_accessed_at": accessed_at,
                                "last_hit_at": None,
                                "hit_count": 0,
                            }
                            cache_evicted_count = _prune_lru_cache(tool_call_cache, cache_max_entries)
                            cache_dirty = True
                except (ImportError, AttributeError) as exc:
                    raise RuntimeError(f"cannot load configured tool {name}: {exc}") from exc
                except _ToolRetryError as exc:
                    latency_ms = round((perf_counter() - start) * 1000, 3)
                    attempt_count = len(exc.attempts)
                    attempts = exc.attempts
                    result = _error_result(name, args, exc.original, latency_ms)
                except Exception as exc:
                    latency_ms = round((perf_counter() - start) * 1000, 3)
                    result = _error_result(name, args, exc, latency_ms)
        content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        message = make_tool_message(call["id"], call["name"], content, result["status"])  #写 ToolMessage、日志和文件
        tool_messages.append(message)
        log_records.append(
            {
                "timestamp": now_iso(),
                "toolset": selected,
                "tool_call_id": call["id"],
                "name": call["name"],
                "status": result["status"],
                "args": call["args"],
                "skill_result": result,
                "latency_ms": result["latency_ms"],
                # Advanced 2: 在不改变 ToolMessage 的情况下，在日志中暴露有限重试行为。
                "attempt_count": attempt_count,
                "retry_attempts": attempts,
                # Advanced 3: 在不改变 ToolMessage 的情况下，在日志中暴露缓存行为。
                "cache_enabled": cache_enabled,
                "cache_hit": cache_hit,
                "cache_key": cache_key,
                "cache_invalidated": cache_invalidated,
                "cache_invalidation_reason": cache_invalidation_reason,
                "cache_evicted_count": cache_evicted_count,
                "cache_dependencies": dependencies,
            }
        )
    if outdir:
        write_json(tool_messages, output_dir / "tool_messages.json")
        for record in log_records:
            append_jsonl(record, output_dir / "tool_call_log.jsonl")
    if cache_dirty:  # Advanced 3: 仅在新增条目或命中计数变化时持久化全局缓存。
        _save_tool_call_cache(tool_call_cache)
    return tool_messages


def _collect_log_paths(log_path: str | None, log_dir: str | None) -> list[Path]:
    # Advanced 4: 支持单个日志文件，或从日志目录递归汇总。
    if bool(log_path) == bool(log_dir):
        raise ValueError("exactly one of --log_path or --log_dir is required with --summarize_log")
    if log_path:
        path = resolve_cli_path(log_path)
        if not path.is_file():
            raise FileNotFoundError(f"tool call log not found: {path}")
        return [path]
    directory = resolve_cli_path(log_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"log directory not found: {directory}")
    paths = sorted(directory.rglob("tool_call_log.jsonl"))
    if not paths:
        raise FileNotFoundError(f"no tool_call_log.jsonl files found under: {directory}")
    return paths


def _empty_tool_stats() -> dict:
    # Advanced 4: 在计算最终指标前，使用紧凑累加器保存每个工具的计数。
    return {
        "total_calls": 0,
        "success_calls": 0,
        "error_calls": 0,
        "latencies": [],
        "attempt_counts": [],
        "cache_enabled_calls": 0,  # Advanced 3: 统计允许使用全局缓存的调用次数。
        "cache_hits": 0,  # Advanced 3: 统计由缓存提供、未运行 Skill 的调用次数。
        "cache_invalidations": 0,  # Advanced 3: 统计因依赖指纹变化而移除的过期条目数量。
        "cache_evictions": 0,  # Advanced 3: 统计有界 LRU 策略移除的条目数量。
    }


def _finalize_tool_stats(stats: dict) -> dict:
    # Advanced 4: 计算失败率、平均耗时和平均尝试次数，用于报告。
    total = stats["total_calls"]
    latencies = stats["latencies"]
    attempts = stats["attempt_counts"]
    return {
        "total_calls": total,
        "success_calls": stats["success_calls"],
        "error_calls": stats["error_calls"],
        "failure_rate": round(stats["error_calls"] / total, 4) if total else 0.0,
        "total_latency_ms": round(sum(latencies), 3) if latencies else 0.0,  # Advanced 4: 暴露工具总执行时间，用于有无缓存的速度对比。
        "avg_latency_ms": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
        "min_latency_ms": min(latencies) if latencies else 0.0,
        "max_latency_ms": max(latencies) if latencies else 0.0,
        "avg_attempt_count": round(sum(attempts) / len(attempts), 3) if attempts else 0.0,
        "cache_enabled_calls": stats["cache_enabled_calls"],  # Advanced 3: 显示启用缓存的日志行数量。
        "cache_hits": stats["cache_hits"],  # Advanced 3: 显示复用已缓存 SkillResult 的日志行数量。
        "cache_hit_rate": round(stats["cache_hits"] / stats["cache_enabled_calls"], 4) if stats["cache_enabled_calls"] else 0.0,  # Advanced 3: 缓存收益比例。
        "cache_invalidations": stats["cache_invalidations"],  # Advanced 3: 显示自动移除过期缓存的次数。
        "cache_evictions": stats["cache_evictions"],  # Advanced 3: 显示 max_entries 导致的 LRU 移除次数。
    }


def summarize_tool_call_logs(log_path: str | None, log_dir: str | None, outdir: str) -> dict:
    # Advanced 4: 将一个或多个 B3 tool_call_log.jsonl 文件汇总为 tool_stats.json。
    log_paths = _collect_log_paths(log_path, log_dir)
    per_tool: dict[str, dict] = {}
    total_records = 0
    for path in log_paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL record in {path}:{line_number}") from exc
            name = record.get("name", "unknown")
            stats = per_tool.setdefault(name, _empty_tool_stats())
            stats["total_calls"] += 1
            total_records += 1
            if record.get("status") == "success":
                stats["success_calls"] += 1
            else:
                stats["error_calls"] += 1
            latency = record.get("latency_ms")
            if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                stats["latencies"].append(latency)
            attempt_count = record.get("attempt_count")
            if isinstance(attempt_count, int) and not isinstance(attempt_count, bool):
                stats["attempt_counts"].append(attempt_count)
            if record.get("cache_enabled") is True:  # Advanced 3: 从 B3 日志汇总缓存资格。
                stats["cache_enabled_calls"] += 1
            if record.get("cache_hit") is True:  # Advanced 3: 从 B3 日志汇总缓存命中。
                stats["cache_hits"] += 1
            if record.get("cache_invalidated") is True:  # Advanced 3: 汇总依赖感知缓存失效。
                stats["cache_invalidations"] += 1
            cache_evicted_count = record.get("cache_evicted_count")  # Advanced 3: 汇总有界 LRU 淘汰数量。
            if isinstance(cache_evicted_count, int) and not isinstance(cache_evicted_count, bool):
                stats["cache_evictions"] += cache_evicted_count
    report = {
        "status": "success",
        "log_files": [str(path) for path in log_paths],
        "total_records": total_records,
        "tools": {name: _finalize_tool_stats(stats) for name, stats in sorted(per_tool.items())},
    }
    output_dir = Path(outdir)
    write_json(report, output_dir / "tool_stats.json")
    return report


def _prepare_cache_benchmark_data(data_root: Path, file_count: int = 30, line_count: int = 5000) -> Path:
    # Advanced 3&4: 创建确定性的本地基准数据，用于可重复的缓存测速。
    benchmark_root = data_root / "cache_speed_benchmark"
    benchmark_root.mkdir(parents=True, exist_ok=True)
    for file_index in range(file_count):
        lines = []
        for line_index in range(line_count):
            lines.append(
                f"doc={file_index} line={line_index} "
                f"agent tool memory search cache benchmark needle_{line_index % 5}\n"
            )
        (benchmark_root / f"doc_{file_index:02d}.txt").write_text("".join(lines), encoding="utf-8")
    return benchmark_root


def _build_cache_benchmark_tool_calls(repeat_count: int = 12) -> list[dict]:
    # Advanced 3&4: 构建更大的重复工作负载，仅含五个唯一键，保持在 10 条 LRU 限制内。
    base_calls = [
        {"name": "local_file_search", "args": {"query": "needle_0", "root_dir": "cache_speed_benchmark", "top_k": 5}},
        {"name": "local_file_search", "args": {"query": "needle_1", "root_dir": "cache_speed_benchmark", "top_k": 5}},
        {"name": "local_file_search", "args": {"query": "needle_2", "root_dir": "cache_speed_benchmark", "top_k": 5}},
        {"name": "local_file_search", "args": {"query": "needle_3", "root_dir": "cache_speed_benchmark", "top_k": 5}},
        {"name": "local_file_search", "args": {"query": "needle_4", "root_dir": "cache_speed_benchmark", "top_k": 5}},
    ]
    calls = []
    for round_index in range(repeat_count):
        for call_index, call in enumerate(base_calls):
            calls.append(
                {
                    "id": f"call_{round_index:02d}_{call_index:02d}",
                    "name": call["name"],
                    "args": dict(call["args"]),
                }
            )
    return calls


def _read_tool_call_log(path: Path) -> list[dict]:
    # Advanced 3&4: 读取基准 JSONL 日志，生成紧凑的单用例速度摘要。
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _reset_benchmark_case_dir(case_dir: Path) -> None:
    # Advanced 3&4: 仅清理基准测试所属的用例目录，保留其他输出产物。
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)


def _run_cache_benchmark_case(
    case_name: str,
    calls: list[dict],
    tools_config: str,
    toolset: str | None,
    outdir: Path,
    use_cache: bool,
) -> dict:
    # Advanced 3&4: 执行一个批量用例，并记录墙钟时间和 B3 单工具耗时。
    case_dir = outdir / case_name
    _reset_benchmark_case_dir(case_dir)
    start = perf_counter()
    execute_tool_calls(calls, tools_config, toolset, str(case_dir), use_cache=use_cache)
    wall_time_ms = round((perf_counter() - start) * 1000, 3)
    records = _read_tool_call_log(case_dir / "tool_call_log.jsonl")
    cache_hits = sum(1 for record in records if record.get("cache_hit") is True)
    return {
        "case": case_name,
        "use_cache": use_cache,
        "log_path": str(case_dir / "tool_call_log.jsonl"),
        "call_count": len(records),
        "success_count": sum(1 for record in records if record.get("status") == "success"),
        "cache_hits": cache_hits,
        "cache_hit_rate": round(cache_hits / len(records), 4) if records else 0.0,
        "wall_time_ms": wall_time_ms,
        "total_latency_ms": round(sum(record.get("latency_ms", 0.0) for record in records), 3),
    }


def run_cache_benchmark(
    tools_config: str,
    toolset: str | None,
    outdir: str,
    compare: bool = False,
    no_cache: bool = False,
) -> dict:
    # Advanced 3&4: 基准模式生成批量工作负载并执行，再复用 Advanced 4 的日志汇总。
    if compare and no_cache:
        raise ValueError("--compare cannot be used together with --no_cache")
    config_path, config = _load_tools_config(tools_config)
    selected, _ = _resolve_toolset(config, toolset)
    data_root_setting = config.get("settings", {}).get("data_root", "../data")
    resolved_data_root = resolve_from_file(data_root_setting, config_path)
    output_dir = Path(outdir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_name in ("benchmark_report.json", "speed_compare_report.json", "tool_stats.json"):
        stale_file = output_dir / stale_name
        if stale_file.exists():
            stale_file.unlink()
    for stale_case_name in ("with_cache", "without_cache"):  # Advanced 3&4: 移除旧基准用例，确保摘要只覆盖本次运行。
        stale_case_dir = output_dir / stale_case_name
        if stale_case_dir.exists():
            shutil.rmtree(stale_case_dir)
    _prepare_cache_benchmark_data(resolved_data_root) #生成测试用数据
    calls = _build_cache_benchmark_tool_calls()
    case_specs = [("without_cache", False)] if no_cache else [("with_cache", True)]
    if compare:
        case_specs = [("with_cache", True), ("without_cache", False)]
    cases = {}
    for case_name, use_cache in case_specs:
        _clear_tool_call_cache()
        cases[case_name] = _run_cache_benchmark_case(case_name, calls, str(config_path), selected, output_dir, use_cache) #循环调用
    tool_stats = summarize_tool_call_logs(None, str(output_dir), str(output_dir))
    comparison = None
    if "with_cache" in cases and "without_cache" in cases:
        with_cache = cases["with_cache"]
        without_cache = cases["without_cache"]
        comparison = {
            "wall_time_speedup": round(without_cache["wall_time_ms"] / with_cache["wall_time_ms"], 3)
            if with_cache["wall_time_ms"]
            else None,
            "total_latency_speedup": round(without_cache["total_latency_ms"] / with_cache["total_latency_ms"], 3)
            if with_cache["total_latency_ms"]
            else None,
        }
    report = {
        "status": "success",
        "mode": "benchmark_cache",
        "toolset": selected,
        "workload": {
            "unique_call_patterns": 5,
            "repeat_count": 12,
            "total_calls_per_case": len(calls),
            "benchmark_data_dir": str(resolved_data_root / "cache_speed_benchmark"),
        },
        "cases": cases,
        "comparison": comparison,
        "log_summary": tool_stats,
        "tool_stats_path": str(output_dir / "tool_stats.json"),
    }
    write_json(report, output_dir / "benchmark_report.json")
    return report


def _default_schema_eval_tool_calls() -> list[dict]:
    # Advanced 5: 提供内置批量调用，包含合法、非法、可选参数和未知工具情况，供 B3 单独评估。
    return [
        {"id": "eval_file_required_only", "name": "file_reader", "args": {"path": "docs/agent_intro.txt"}},
        {
            "id": "eval_file_with_optional",
            "name": "file_reader",
            "args": {"path": "docs/agent_intro.txt", "max_chars": 120},
        },
        {
            "id": "eval_search_required_only",
            "name": "local_file_search",
            "args": {"query": "Agent"},
        },
        {
            "id": "eval_search_with_optional",
            "name": "local_file_search",
            "args": {"query": "tool", "root_dir": "docs", "top_k": 3},
        },
        {
            "id": "eval_format_required_only",
            "name": "format_converter",
            "args": {"text": "name: agent\nstatus: ready", "target_format": "json"},
        },
        {
            "id": "eval_format_with_optional",
            "name": "format_converter",
            "args": {
                "text": "alpha\nbeta",
                "target_format": "markdown",
                "output_filename": "advanced5_format.md",
            },
        },
        {
            "id": "eval_table_with_optional",
            "name": "table_analyzer",
            "args": {"path": "tables/results.csv", "max_rows_preview": 2, "describe": True},
        },
        {"id": "eval_missing_required", "name": "file_reader", "args": {"max_chars": 100}},
        {"id": "eval_wrong_type", "name": "local_file_search", "args": {"query": 123}},
        {"id": "eval_extra_arg", "name": "file_reader", "args": {"path": "docs/agent_intro.txt", "limit": 10}},
        {"id": "eval_unknown_tool", "name": "unknown_tool", "args": {}},
    ]


def _load_schema_eval_tool_calls(eval_tool_calls: str | None) -> list[dict]:
    # Advanced 5: 允许 CLI 提供批量调用；否则使用内置 B3 评估工作负载。
    if not eval_tool_calls:
        return _default_schema_eval_tool_calls()
    payload = read_json(resolve_cli_path(eval_tool_calls))
    calls = payload.get("tool_calls") if isinstance(payload, dict) else payload
    if not isinstance(calls, list):
        raise ValueError("eval tool calls must be a list or an object with tool_calls")
    return calls


def _schema_tool_map(schema: list[dict]) -> dict[str, dict]:
    # Advanced 5: 按函数名索引已生成的 tools_schema 条目，用于方案专属校验。
    result = {}
    for entry in schema:
        function = entry.get("function") if isinstance(entry, dict) else None
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            result[function["name"]] = function
    return result


def _validate_args_against_schema(args: dict, parameter_schema: dict) -> None:
    # Advanced 5: 根据所选 Schema 方案评估 tool_call 兼容性，而不只依据 tools.yaml。
    if not isinstance(args, dict):
        raise ValueError("args must be an object")
    properties = parameter_schema.get("properties", {})
    required = parameter_schema.get("required", [])
    missing = [name for name in required if name not in args]
    if missing:
        raise ValueError(f"missing required parameters: {', '.join(missing)}")
    if parameter_schema.get("additionalProperties") is False:
        unknown = sorted(set(args) - set(properties))
        if unknown:
            raise ValueError(f"unknown parameters: {', '.join(unknown)}")
    for name, value in args.items():
        if name not in properties:
            continue
        expected_name = properties[name].get("type", "string")
        expected = JSON_TYPES.get(expected_name)
        if expected is None:
            continue
        if expected_name in {"integer", "number"} and isinstance(value, bool):
            valid = False
        else:
            valid = isinstance(value, expected)
        if not valid:
            raise ValueError(f"parameter {name} must be {expected_name}")
        item_schema = properties[name].get("items")
        if expected_name == "array" and isinstance(item_schema, dict):
            item_type = item_schema.get("type")
            if item_type in JSON_TYPES and not all(isinstance(item, JSON_TYPES[item_type]) for item in value):
                raise ValueError(f"parameter {name} contains invalid items")


def _variant_schema_validation(schema: list[dict], tool_calls: list[dict]) -> tuple[list[dict], list[dict]]:
    # Advanced 5: 将批量 tool_calls 拆分为 Schema 合法调用和结构化 Schema 校验错误。
    tool_map = _schema_tool_map(schema)
    valid_calls = []
    records = []
    for index, raw_call in enumerate(tool_calls):
        try:
            call = normalize_tool_call(raw_call, index)
            function = tool_map.get(call["name"])
            if not function:
                raise ValueError(f"tool is not declared in schema: {call['name']}")
            _validate_args_against_schema(call["args"], function.get("parameters", {}))
            valid_calls.append(call)
            records.append(
                {
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "args": call["args"],
                    "schema_valid": True,
                    "error": None,
                }
            )
        except Exception as exc:
            fallback_name = raw_call.get("name", "unknown") if isinstance(raw_call, dict) else "unknown"
            fallback_args = raw_call.get("args", {}) if isinstance(raw_call, dict) else {}
            records.append(
                {
                    "tool_call_id": raw_call.get("id", f"call_{index + 1:03d}") if isinstance(raw_call, dict) else f"call_{index + 1:03d}",
                    "name": fallback_name,
                    "args": fallback_args,
                    "schema_valid": False,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            )
    return valid_calls, records


def evaluate_schema_variants(
    tools_config: str,
    toolset: str | None,
    outdir: str,
    eval_tool_calls: str | None = None,
) -> dict:
    # Advanced 5: 使用同一批构造的 tool_calls 比较 auto/minimal/detailed Schema 设计。
    config_path, config = _load_tools_config(tools_config)
    selected, _ = _resolve_toolset(config, toolset)
    output_dir = Path(outdir)
    output_dir.mkdir(parents=True, exist_ok=True)
    tool_calls = _load_schema_eval_tool_calls(eval_tool_calls)
    write_json({"tool_calls": tool_calls}, output_dir / "schema_eval_tool_calls.json")
    variants = {}
    for schema_variant in ("auto", "minimal", "detailed"):
        variant_dir = output_dir / schema_variant
        _reset_benchmark_case_dir(variant_dir)
        schema = get_tools_schema(str(config_path), selected, str(output_dir), schema_variant=schema_variant)
        valid_calls, validation_records = _variant_schema_validation(schema, tool_calls)
        write_json(validation_records, variant_dir / "schema_validation.json")
        if valid_calls:
            execute_tool_calls(valid_calls, str(config_path), selected, str(variant_dir), use_cache=False)
            tool_stats = summarize_tool_call_logs(str(variant_dir / "tool_call_log.jsonl"), None, str(variant_dir))
        else:
            tool_stats = {"status": "success", "log_files": [], "total_records": 0, "tools": {}}
            write_json(tool_stats, variant_dir / "tool_stats.json")
        schema_valid_count = sum(1 for record in validation_records if record["schema_valid"])
        executed_success_count = 0
        executed_error_count = 0
        for stats in tool_stats.get("tools", {}).values():
            executed_success_count += stats.get("success_calls", 0)
            executed_error_count += stats.get("error_calls", 0)
        total_cases = len(validation_records)
        variants[schema_variant] = {
            "schema_file": str(output_dir / _schema_output_filename(schema_variant)),
            "validation_path": str(variant_dir / "schema_validation.json"),
            "tool_stats_path": str(variant_dir / "tool_stats.json"),
            "total_cases": total_cases,
            "schema_valid_count": schema_valid_count,
            "schema_invalid_count": total_cases - schema_valid_count,
            "schema_valid_rate": round(schema_valid_count / total_cases, 4) if total_cases else 0.0,
            "executed_success_count": executed_success_count,
            "executed_error_count": executed_error_count,
            "execution_success_rate": round(executed_success_count / schema_valid_count, 4) if schema_valid_count else 0.0,
        }
    report = {
        "status": "success",
        "mode": "evaluate_schema",
        "toolset": selected,
        "input_tool_calls_path": str(output_dir / "schema_eval_tool_calls.json"),
        "variants": variants,
        "notes": [
            "auto keeps the original Advanced 1 schema output name tools_schema.json.",
            "minimal and detailed are comparison schemas and do not replace the default B1/B4 schema.",
            "This B3-only evaluation uses constructed batch tool_calls; B4 prompt_json can later supply model-generated calls through --eval_tool_calls.",
        ],
    }
    write_json(report, output_dir / "schema_eval_report.json")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate tool schema or execute tool calls.")
    parser.add_argument("--tools_config")  # Advanced 4: 仅 Schema 导出/执行模式必需。
    parser.add_argument("--toolset", default=None)
    parser.add_argument("--tool_calls")
    parser.add_argument("--eval_tool_calls")  # Advanced 5: 用于 Schema 对比评估的可选批量 tool_call 文件。
    parser.add_argument("--schema_variant", choices=sorted(SCHEMA_VARIANTS), default="auto")  # Advanced 5: 导出/使用 auto、minimal 或 detailed Schema。
    parser.add_argument("--log_path")  # Advanced 4: 汇总单个 tool_call_log.jsonl 文件。
    parser.add_argument("--log_dir")  # Advanced 4: 汇总目录下的全部 tool_call_log.jsonl 文件。
    parser.add_argument("--no_cache", action="store_true")  # Advanced 3: 仅为本次 CLI 执行禁用全局缓存。
    parser.add_argument("--compare", action="store_true")  # Advanced 3&4: 同时基准测试有缓存和无缓存用例。
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--export_schema", action="store_true")
    action.add_argument("--execute", action="store_true")
    action.add_argument("--summarize_log", action="store_true")  # Advanced 4: 生成 tool_stats.json。
    action.add_argument("--benchmark_cache", action="store_true")  # Advanced 3&4: 生成批量工作负载并汇总缓存速度。
    action.add_argument("--evaluate_schema", action="store_true")  # Advanced 5: 在批量 tool_calls 上比较 auto/minimal/detailed Schema。
    action.add_argument("--clear_cache", action="store_true")  # Advanced 3: 删除生成的全局缓存文件。
    parser.add_argument("--outdir")  # Advanced 3: 产生输出的模式必需；--clear_cache 不需要。
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.clear_cache:  # Advanced 3: 清理缓存不依赖 tools.yaml/outdir。
            result = _clear_tool_call_cache()
            print(result["cache_path"])
            return 0
        if args.no_cache and not (args.execute or args.benchmark_cache):  # Advanced 3&4: --no_cache 仅影响直接执行或基准模式。
            raise ValueError("--no_cache is only valid with --execute or --benchmark_cache")
        if args.compare and not args.benchmark_cache:  # Advanced 3&4: --compare 仅在基准模式中有意义。
            raise ValueError("--compare is only valid with --benchmark_cache")
        if args.eval_tool_calls and not args.evaluate_schema:  # Advanced 5: 评估批量输入仅由 Schema 评估使用。
            raise ValueError("--eval_tool_calls is only valid with --evaluate_schema")
        if not args.outdir:  # Advanced 3: 保持原输出模式显式指定 outdir，同时不强制 --clear_cache 指定。
            raise ValueError("--outdir is required with --export_schema, --execute, --summarize_log, --benchmark_cache, or --evaluate_schema")
        outdir = resolve_cli_path(args.outdir)
        if args.summarize_log:  # Advanced 4: 日志汇总不需要 tools.yaml。
            summarize_tool_call_logs(args.log_path, args.log_dir, str(outdir))
            print(outdir / "tool_stats.json")
        else:
            if not args.tools_config:  # Advanced 3&4: 保持需要工具配置的模式的参数要求。
                raise ValueError("--tools_config is required with --export_schema, --execute, --benchmark_cache, or --evaluate_schema")
            config_path = resolve_cli_path(args.tools_config)
            if args.export_schema:
                if not args.toolset:
                    _, config = _load_tools_config(config_path)
                    args.toolset = config.get("default_toolset")
                get_tools_schema(str(config_path), args.toolset, str(outdir), schema_variant=args.schema_variant)
                print(outdir / _schema_output_filename(args.schema_variant))
            elif args.benchmark_cache:  # Advanced 3&4: 基准默认使用缓存；--compare 同时运行两者；--no_cache 只运行无缓存。
                run_cache_benchmark(str(config_path), args.toolset, str(outdir), compare=args.compare, no_cache=args.no_cache)
                print(outdir / "benchmark_report.json")
            elif args.evaluate_schema:  # Advanced 5: 始终使用同一批调用比较 auto/minimal/detailed。
                evaluate_schema_variants(str(config_path), args.toolset, str(outdir), args.eval_tool_calls)
                print(outdir / "schema_eval_report.json")
            else:
                if not args.tool_calls:
                    raise ValueError("--tool_calls is required with --execute")
                payload = read_json(resolve_cli_path(args.tool_calls))
                tool_calls = payload.get("tool_calls") if isinstance(payload, dict) else payload
                execute_tool_calls(tool_calls, str(config_path), args.toolset, str(outdir), use_cache=not args.no_cache)  # Advanced 3: CLI 可退出缓存，而导入调用默认启用缓存。
                print(outdir / "tool_messages.json")
        return 0
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
