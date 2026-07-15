# B2-B3 Personal Module Submission

本仓库是 Agent 方向实训的个人模块提交，包含 B2 Skill Function Layer 与 B3 Tool Call Layer 的实现、配置、样例数据和独立演示命令。B2 负责提供可重复执行的本地能力；B3 负责把模型生成的工具调用转换为安全、可校验、可记录的 Skill 执行流程。

## 1. 模块概述

### 1.1 模块名称

- **B2 - Skill Function Layer**
- **B3 - Tool Call Layer**

### 1.2 模块说明

B2 将文件读取、文件检索、表格分析、格式转换和安全计算等本地能力封装为统一 Skill。每个 Skill 接收 JSON 参数，返回统一 `SkillResult`；异常也会转换为稳定的结构化错误信息。

B3 是 B2 与模型/运行时之间的调度层。它从 `tools.yaml` 读取工具注册信息，生成 JSON Schema 供 B1/B4 使用，验证 `tool_call` 参数，调用对应 B2 Skill，并返回 ToolMessage、执行日志和统计报告。

```text
B4 模型输出 AIMessage.tool_calls
        -> B3 Schema 校验、缓存、重试和调度
        -> B2 本地 Skill 执行
        -> SkillResult / ToolMessage / JSONL 日志
        -> B4 使用结果生成最终回答
```

### 1.3 完成情况概览

| 类型 | 完成情况 |
|---|---|
| 基础要求 | 已完成 5 个基础 Skill、统一 SkillResult、工具 Schema、参数校验、ToolMessage 与执行日志。 |
| B2 进阶要求 | 已完成 Advanced1 加权关键词检索、Advanced3 固定复合 Skill、Advanced4 结构化错误码。 |
| B3 进阶要求 | 已完成 Advanced1 自动 Schema、Advanced2 重试、Advanced3 依赖感知 LRU 缓存、Advanced4 Benchmark、Advanced5 Schema 对比评估。 |
| 可独立运行的演示 | 可通过 `b2_run_skill.py` 和 `b3_tool_layer.py` 在无模型、无 GPU 的环境中完成演示。 |
| 与团队系统集成情况 | `tools_schema.json` 提供给 B1/B4；B3 接收 B4 的 `tool_calls` 并调度 B2，返回 ToolMessage。 |

## 2. 环境、模型与数据依赖

### 2.1 运行环境

| 项目 | 要求 |
|---|---|
| 推荐 Python 版本 | Python 3.10（服务器验证环境）；B2/B3 静态功能已在 Python 3.13.5 本地验证。 |
| 必要依赖 | `PyYAML==6.0.3`；完整依赖见 `requirements.txt`。 |
| 是否需要模型 | B2/B3 独立运行、缓存测试和静态 Schema 评估不需要模型。 |
| 是否需要 GPU | 不需要。 |
| 是否需要外部数据集 | 不需要；仓库内含可运行的小型文档、表格和 JSON 样例。 |

### 2.2 模型依赖

B2/B3 的独立测试不加载模型。`code/b3_b4_real_schema_eval.py` 用于 Advanced5 的真实模型评估，它需要团队 B4 模块提供的本地模型配置和权重；模型文件体积较大且不属于个人模块仓库，因此未上传。

### 2.3 样例数据依赖

| 数据或文件 | 来源 | 项目内相对路径 | 用途 |
|---|---|---|---|
| Agent 文档样例 | 项目自构造 | `data/docs/` | 验证文件读取、加权检索、搜索后读取。 |
| CSV 表格样例 | 项目自构造 | `data/tables/results.csv` | 验证表格统计和格式转换。 |
| B2 输入样例 | 项目自构造 | `data/tool_inputs/` | 验证基础 Skill、复合 Skill 与错误处理。 |
| B3 Tool Call 样例 | 项目自构造 | `data/messages/` | 验证 Schema、参数校验、缓存与重试。 |
| Schema 评估案例 | 项目自构造 | `data/schema_eval/b4_real_eval_cases.json` | 供 B3/B4 真实模型 Schema 评估使用。 |

### 2.4 安装步骤

```bash
conda create -n agent python=3.10 -y
conda activate agent
pip install -r requirements.txt
```

如果只演示 B2/B3 的无模型功能，安装 `PyYAML` 后即可运行：

```bash
pip install PyYAML==6.0.3
```

## 3. 文件结构与接口边界

### 3.1 文件结构

```text
personal_sample/
├── code/
│   ├── b2_run_skill.py              # B2 单 Skill CLI 与动态调度入口
│   ├── b3_tool_layer.py             # B3 Schema、校验、执行、缓存和统计入口
│   ├── b3_cache_speed_test.py       # B3 缓存测速辅助脚本
│   ├── b3_b4_real_schema_eval.py    # B3-B4 真实模型 Schema 评估脚本
│   └── common/                      # JSON、路径、日志和 SkillResult 公共工具
├── skills/                          # 5 个基础 Skill 与 3 个复合 Skill
├── configs/tools.yaml               # 工具注册、隐藏参数、重试和缓存配置
├── data/
│   ├── docs/                        # 文本/Markdown 文档样例
│   ├── tables/                      # CSV 表格样例
│   ├── tool_inputs/                 # B2 CLI 输入 JSON
│   ├── messages/                    # B3 Tool Call JSON
│   └── schema_eval/                 # Schema 评估案例
├── requirements.txt                 # Python 依赖
├── .gitignore                       # 忽略模型、输出和本地缓存
└── README.md                        # 本说明文档
```

### 3.2 接口边界

| 类型 | 来源 / 去向 | 数据格式 | 说明 |
|---|---|---|---|
| B2 输入 | B2 CLI 或 B3 | JSON 参数对象 | 例如 `{\"path\": \"docs/agent_intro.txt\", \"max_chars\": 2000}`。 |
| B2 输出 | B3 | `SkillResult` JSON | 固定包含 `skill_name`、`status`、`input`、`output`、`error`、`latency_ms`。 |
| B3 输入 | B4 / 测试 JSON | `AIMessage.tool_calls` | 每项包含 `id`、`name`、`args`。 |
| B3 Schema 输出 | B1 / B4 | OpenAI 风格工具 JSON Schema | 默认文件名为 `tools_schema.json`，描述可用工具、参数类型和必填项。 |
| B3 执行输出 | B4 / 输出目录 | ToolMessage JSON、JSONL 日志 | `tool_messages.json` 供模型读取，`tool_call_log.jsonl` 用于追踪、缓存与统计。 |

## 4. 基础要求实现与演示

### 4.1 基础功能说明

B2 基础层提供以下确定性本地能力：

| Skill | 主要功能 | 典型输入 | 主要输出 |
|---|---|---|---|
| `calculator` | 安全计算四则表达式 | `expression` | 数值 `result`。 |
| `file_reader` | 读取 UTF-8 `txt/md` 文件 | `path`、`max_chars` | 文本、字符数、截断标记。 |
| `local_file_search` | 在本地文档中检索关键词 | `query`、`root_dir`、`top_k` | 排序后的路径、分数与片段。 |
| `table_analyzer` | 分析 CSV/TSV 表格 | `path`、`describe` | 行列数、列名、预览、数值统计。 |
| `format_converter` | 转为 Markdown 或 JSON 文件 | `text`、`target_format` | 转换文本与生成文件路径。 |

B3 基础层完成以下流程：加载 `tools.yaml`、导出工具 Schema、验证工具名与参数、动态导入 B2 Skill、注入 `data_root/output_dir` 等框架参数、生成 ToolMessage，并记录调用日志。

```text
Tool Call JSON -> 工具名与参数校验 -> 动态加载 B2 函数
-> 注入运行时参数 -> 执行 Skill -> SkillResult -> ToolMessage + JSONL
```

### 4.2 基础功能实现路径

| 文件 / 函数 / 脚本 | 作用 |
|---|---|
| `code/b2_run_skill.py::run_skill()` | 根据 `SKILL_MODULES` 动态导入指定 Skill，按函数签名注入运行时参数并统一封装结果。 |
| `skills/__init__.py::resolve_data_path()` | 解析相对于 `data/` 的路径，并阻止越界访问。 |
| `code/common/schemas.py::make_skill_result()` | 创建统一的 SkillResult 结构。 |
| `code/b3_tool_layer.py::get_tools_schema()` | 根据 `tools.yaml` 和 Python 函数签名生成工具 Schema。 |
| `code/b3_tool_layer.py::execute_tool_calls()` | 执行完整 B3 调度流程，产生 ToolMessage 和调用日志。 |

### 4.3 基础功能输入格式与样例

| 字段 / 输入文件 | 类型 / 格式 | 是否必需 | 说明 |
|---|---|---|---|
| `--skill` | CLI 字符串 | 是 | B2 要运行的 Skill 名称。 |
| `--input` | JSON 文件路径 | 是 | B2 Skill 参数对象。 |
| `--outdir` | 目录路径 | 是 | B2 结果 JSON 与日志输出目录。 |
| `--tools_config` | YAML 文件路径 | B3 执行/导出时是 | 工具注册与策略配置。 |
| `--toolset` | 字符串 | B3 执行/导出时是 | 当前使用的工具集合，如 `basic_tools`。 |
| `--tool_calls` | JSON 文件路径 | B3 执行时是 | AIMessage 或 Tool Call 列表。 |

| 样例文件 | 用途 |
|---|---|
| `data/tool_inputs/tool_input_file_reader.json` | 验证基础文件读取。 |
| `data/tool_inputs/tool_input_calculator.json` | 验证安全计算。 |
| `data/messages/ai_message_with_tool_calls.json` | 验证 B3 对 `file_reader` 的完整调度。 |

### 4.4 基础功能演示命令

在仓库根目录执行：

```bash
cd code

python b2_run_skill.py --skill file_reader --input ../data/tool_inputs/tool_input_file_reader.json --outdir ../outputs/basic_file_reader

python b3_tool_layer.py --export_schema --tools_config ../configs/tools.yaml --toolset basic_tools --outdir ../outputs/basic_schema

python b3_tool_layer.py --execute --tools_config ../configs/tools.yaml --toolset basic_tools --tool_calls ../data/messages/ai_message_with_tool_calls.json --outdir ../outputs/basic_b3_execute
```

观察点：

- B2 命令生成 `file_reader_result.json` 和 `skill_run_log.jsonl`，`status` 为 `success`。
- Schema 命令生成 `tools_schema.json`，其中包含 `basic_tools` 内的工具定义。
- B3 命令生成 `tool_messages.json` 与 `tool_call_log.jsonl`，B2 的 SkillResult 会被封装为 ToolMessage。

### 4.5 基础功能输出格式

| 输出文件 / 返回字段 | 格式 | 说明 |
|---|---|---|
| `<skill>_result.json` | JSON | B2 单 Skill 完整执行结果。 |
| `skill_run_log.jsonl` | JSONL | B2 每次 CLI 调用追加一行。 |
| `tools_schema.json` | JSON | B3 导出的工具说明书。 |
| `tool_messages.json` | JSON | B3 返回给 B4 的工具执行消息。 |
| `tool_call_log.jsonl` | JSONL | B3 每次 Tool Call 的状态、耗时、重试、缓存信息。 |

## 5. 进阶要求实现与演示

### 5.1 选择的进阶要求

| 进阶要求 | 是否完成 | 对应文件 / 函数 | 简要说明 |
|---|---|---|---|
| B2 Advanced1 | 是 | `skills/local_file_search.py` | 完整短语、正文词项和文件名词项的可解释加权检索。 |
| B2 Advanced3 | 是 | `skills/read_and_convert.py` 等 | 三个固定复合 Skill。 |
| B2 Advanced4 | 是 | `skills/__init__.py::skill_error_payload()` | 统一错误码、类别和可重试标记。 |
| B3 Advanced1 | 是 | `_auto_parameter_schema()` | 从 Python 签名自动生成 Schema。 |
| B3 Advanced2 | 是 | `_call_tool_with_retry()` | 基于 YAML 的有限重试。 |
| B3 Advanced3 | 是 | 缓存相关函数与 `execute_tool_calls()` | 依赖感知全局 LRU 缓存。 |
| B3 Advanced4 | 是 | `run_cache_benchmark()` | 批量缓存测速和日志统计。 |
| B3 Advanced5 | 是 | `evaluate_schema_variants()` | auto/minimal/detailed Schema 对比评估。 |

### 5.2 B2 Advanced1：可解释加权关键词检索

`local_file_search` 同时保留完整查询短语并按空格拆分词项。它按下式计算分数，再按“分数降序、路径升序”输出：

```text
score = 完整短语命中次数 × 10
      + 正文词项出现次数之和 × 3
      + 文件名词项出现次数之和 × 5
      + 命中的不同词项数量
```

输出中的 `score`、`match_type`、`matched_terms` 和 `snippet` 使检索结果可解释；`require_all_terms` 可要求所有词项命中以减少噪声。

```bash
cd code
python b2_run_skill.py --skill local_file_search --input ../data/tool_inputs/tool_input_file_search.json --outdir ../outputs/B2_advanced1
```

本机实测查询 `Agent 工具调用` 命中 `docs/agent_intro.txt`，返回 `score=16`、`match_type=terms` 和匹配词 `agent`、`工具调用`。

### 5.3 B2 Advanced3：固定复合 Skill

为降低模型手动串联常见两步工具的复杂度，实现三个固定复合 Skill：

| 复合 Skill | 内部调用链 | 输出重点 |
|---|---|---|
| `read_and_convert` | `file_reader -> format_converter` | 源文本、转换结果、生成文件。 |
| `search_and_read` | `local_file_search -> file_reader` | 搜索结果、选中文件与内容。 |
| `analyze_and_convert_table` | `table_analyzer -> format_converter` | 表格统计和 Markdown/JSON 报告。 |

```bash
cd code
python b2_run_skill.py --skill read_and_convert --input ../data/tool_inputs/tool_input_read_and_convert.json --outdir ../outputs/B2_advanced3/read_and_convert
python b2_run_skill.py --skill search_and_read --input ../data/tool_inputs/tool_input_search_and_read.json --outdir ../outputs/B2_advanced3/search_and_read
python b2_run_skill.py --skill analyze_and_convert_table --input ../data/tool_inputs/tool_input_analyze_and_convert_table.json --outdir ../outputs/B2_advanced3/analyze_and_convert_table
```

本机三项调用均成功：`search_and_read` 选中 `docs/tool_calling.md`（分数 28）；表格复合 Skill 正确得到 3 行、3 列，`score` 平均值为 92.67。

### 5.4 B2 Advanced4：统一错误码与可恢复性标记

`skill_error_payload()` 将原始 Python 异常映射为稳定的 `error` 对象，避免上层依赖不稳定的异常文本：

```json
{
  "type": "FileNotFoundError",
  "code": "FILE_NOT_FOUND",
  "category": "file",
  "message": "file not found: docs/missing.txt",
  "retryable": false,
  "details": {}
}
```

已覆盖 `FILE_NOT_FOUND`、`PERMISSION_DENIED`、`TIMEOUT`、`IO_ERROR`、`TYPE_ERROR`、`VALUE_ERROR`、`PATH_ESCAPE` 和 `UNKNOWN_ERROR`。其中 `retryable` 供 B3 判断是否应进入重试流程。

```bash
cd code
python b2_run_skill.py --skill file_reader --input ../data/tool_inputs/tool_input_file_reader_error.json --outdir ../outputs/B2_advanced4/file_reader_error
python b2_run_skill.py --skill format_converter --input ../data/tool_inputs/tool_input_format_converter_error.json --outdir ../outputs/B2_advanced4/format_converter_error
```

本机实测分别返回 `FILE_NOT_FOUND` 与 `VALUE_ERROR`，均以 `status=error` 正常结束，CLI 不会崩溃。

### 5.5 B3 Advanced1 与 Advanced2：自动 Schema 和有限重试

Advanced1 使用 `inspect.signature()` 和类型注解推导公开参数的 `properties`、`required` 和 JSON 类型；`data_root`、`output_dir` 等框架参数由 `hidden_parameters` 隐藏，B3 执行时自动注入。这样修改 B2 函数签名后重新导出 Schema 即可同步，避免手写 Schema 漂移。

Advanced2 从 `tools.yaml` 读取 `retry.max_attempts` 和 `retry.retry_on`。例如 `OSError` 可在上限内重试，而参数错误、未知工具和类型错误不会重复执行。

```bash
cd code
python b3_tool_layer.py --export_schema --tools_config ../configs/tools.yaml --toolset basic_tools --outdir ../outputs/B3_advanced1
python b3_tool_layer.py --execute --tools_config ../configs/tools.yaml --toolset basic_tools --tool_calls ../data/messages/b3_tool_call_flaky_retry_probe.json --outdir ../outputs/B3_advanced2
```

检查 `tools_schema.json` 可验证自动生成的参数定义；检查 `tool_call_log.jsonl` 的 `attempt_count` 和 `retry_attempts` 可验证重试过程。

### 5.6 B3 Advanced3：依赖感知全局 LRU 缓存

缓存键是规范化 `name + args` 的 SHA256 哈希。每条缓存同时保存 SkillResult 及依赖文件指纹 `path`、`exists`、`mtime_ns`、`size`。当数据文件被修改、删除或新建时，当前指纹与缓存不一致，B3 自动使该条缓存失效并重新调用 Skill。

缓存文件为 `outputs/B3_tool_cache/tool_call_cache.json`，最大容量由 `tools.yaml` 的 `settings.cache.max_entries` 设置为 10；超过上限时按 LRU 淘汰最久未访问的条目。缓存读写由互斥锁保护，可用 `--no_cache` 临时关闭，用 `--clear_cache` 清空。

```bash
cd code
python b3_tool_layer.py --clear_cache
python b3_tool_layer.py --execute --tools_config ../configs/tools.yaml --toolset basic_tools --tool_calls ../data/messages/ai_message_with_tool_calls.json --outdir ../outputs/B3_advanced3
python b3_tool_layer.py --execute --tools_config ../configs/tools.yaml --toolset basic_tools --tool_calls ../data/messages/ai_message_with_tool_calls.json --outdir ../outputs/B3_advanced3
```

两次运行后查看 `outputs/B3_advanced3/tool_call_log.jsonl`：首次记录 `cache_hit=false`、`attempt_count=1`；第二次记录 `cache_hit=true`、`attempt_count=0`。

### 5.7 B3 Advanced4：批量 Benchmark 与日志统计

Benchmark 自动构造 30 个大文本文件，生成 5 种不同 `local_file_search` 调用模式，每种重复 12 次。因此有缓存和无缓存分支各执行 60 条调用。`--compare` 会输出 `benchmark_report.json` 和 `tool_stats.json`，统计成功率、缓存命中率、累计耗时、墙钟时间与加速比。

```bash
cd code
python b3_tool_layer.py --benchmark_cache --compare --tools_config ../configs/tools.yaml --toolset basic_tools --outdir ../outputs/B3_advanced4
```

本机实测结果：With Cache 为 60/60 成功、命中 55 次、命中率 0.9167、墙钟时间 1737.937 ms；Without Cache 为 60/60 成功、墙钟时间 2518.474 ms。缓存的墙钟加速比为 **1.449x**，累计工具耗时加速比为 **1.470x**。

### 5.8 B3 Advanced5：Schema 描述对比评估

该功能保留 `tools_schema.json` 作为 B1/B4 默认自动 Schema，同时生成 `tools_schema_minimal.json` 和 `tools_schema_detailed.json`。三者拥有完全相同的工具和参数约束，只改变描述详略，避免因删减必填参数造成不公平比较。

```bash
cd code
python b3_tool_layer.py --evaluate_schema --tools_config ../configs/tools.yaml --toolset basic_tools --outdir ../outputs/B3_advanced5_static
```

静态评估构造合法调用、缺参、类型错误、多余参数和未知工具等批量样例。三种 Schema 均完成 11 条校验，其中 7 条合法、4 条被正确拒绝，`schema_valid_rate=0.6364`；7 条合法调用均由 B3 成功执行，`execution_success_rate=1.000`。

真实模型评估由 `b3_b4_real_schema_eval.py` 联合 B4 的 `prompt_json` 模式执行。该模式需要团队 B4 模型环境；在既有 8 条自然语言案例中，`auto` 方案端到端 Tool Call 准确率为 0.625，高于 `minimal` 的 0.500 和 `detailed` 的 0.250。

## 6. 与团队系统的集成说明

1. **B1/B4 获取工具能力**：B1 或 B4 调用 `b3_tool_layer.get_tools_schema()`，默认得到 `tools_schema.json`。该 JSON Schema 描述 `basic_tools` 中的工具名、公开参数、类型和必填项。
2. **B4 产生工具调用**：B4 的真实模型在 `prompt_json` 模式下输出 AIMessage，其中 `tool_calls` 包含 `id`、`name` 和 `args`。
3. **B3 执行调度**：B1 运行时将 Tool Call 传给 `execute_tool_calls()`。B3 读取 `tools.yaml`，校验参数、判断缓存、按需要重试，并动态调用 B2 Skill。
4. **B2 返回统一结果**：B2 返回 SkillResult；B3 将其序列化为 ToolMessage，保存 `tool_messages.json` 与 `tool_call_log.jsonl`。
5. **B4 生成最终回答**：B1 把 ToolMessage 放回消息链，B4 使用工具结果回答用户问题。

联调中保持了两项接口约束：第一，B2 的 `SkillResult` 始终包含统一外层字段；第二，B3 默认 Schema 文件名始终为 `tools_schema.json`，`minimal/detailed` 仅用于对照评估，不替换 B1/B4 的默认接口。

## 7. 已知问题与后续改进

| 问题 | 当前原因 | 后续改进 |
|---|---|---|
| B3 的真实模型评估不能在纯 B2/B3 环境独立完成 | 该功能需要 B4 模型、权重和 GPU 环境。 | 通过轻量级远程模型接口或模拟模型输出补充可移植演示。 |
| 规则式关键词检索不理解同义词和语义相近表达 | 当前实现强调确定性、可解释性和无模型运行。 | 增加可选的向量检索或 BM25 检索后端，并保留当前规则评分作为回退方案。 |
| 全局缓存存储在 JSON 文件 | 适合课程规模的 10 条 LRU 演示，但不适合高并发大规模服务。 | 使用 SQLite/Redis 等带锁和过期机制的持久化缓存。 |
| Schema 描述详略对小模型的效果不稳定 | 真实模型的工具决策受提示词、模型能力和案例难度共同影响。 | 扩充评测集，保留原始模型输出，并比较更多模型和提示模板。 |
