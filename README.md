# B2-B3 Personal Module Submission

This repository contains the personal implementation for the Agent project's B2 Skill Function Layer and B3 Tool Call Layer.

## Modules

- **B2**: deterministic local Skills, composite Skills, weighted keyword search, and structured error codes.
- **B3**: JSON Schema generation, argument validation, retry control, dependency-aware LRU caching, benchmark statistics, and Schema evaluation.

## Environment

```bash
pip install -r requirements.txt
```

The project uses the sample files under `data/`. Model files are intentionally not included. The B3 static tests run locally without a model; the real-model Schema evaluation additionally requires the team B4 model environment.

## Quick Start

```bash
cd code

python b2_run_skill.py --skill local_file_search --input ../data/tool_inputs/tool_input_file_search.json --outdir ../outputs/B2_advanced1

python b3_tool_layer.py --benchmark_cache --compare --tools_config ../configs/tools.yaml --toolset basic_tools --outdir ../outputs/B3_advanced4
```

## Main Entry Points

- `code/b2_run_skill.py`: B2 Skill CLI.
- `code/b3_tool_layer.py`: B3 Schema, execution, cache, benchmark, and evaluation CLI.
- `configs/tools.yaml`: tool registration and retry configuration.
- `skills/`: basic and composite Skill implementations.
