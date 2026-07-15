from __future__ import annotations

from pathlib import Path

from b3_tool_layer import run_cache_benchmark


BASE_DIR = Path(__file__).resolve().parents[1]
TOOLS_CONFIG = BASE_DIR / "configs" / "tools.yaml"
OUTPUT_ROOT = BASE_DIR / "outputs" / "B3_cache_speed_compare"


def main() -> int:
    # Advanced 3&4: 将该独立脚本保留为统一 CLI 基准测试实现的轻量封装。
    run_cache_benchmark(str(TOOLS_CONFIG), "basic_tools", str(OUTPUT_ROOT), compare=True)
    print(OUTPUT_ROOT / "benchmark_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
