"""路径与常量：样例目录、运行记录目录（文件持久化，可重跑）。"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Parquet 字段演进映射 · schemaevo"
BASE_DIR = Path(__file__).resolve().parent.parent
SAMPLE_DIR = Path(os.environ.get("SAMPLE_DIR", BASE_DIR / "sample_data"))
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
RUN_DIR = DATA_DIR / "runs"
MATRIX_VERSION = "type-matrix/2026.11"
SAMPLE_KEYS = ("A", "B", "C")
MAX_RUNS_KEPT = 200

# 路径别名（结构搬层）：样例、版本化映射与运行记录
ALIAS_SAMPLE_DIR = SAMPLE_DIR / "alias"
ALIAS_SAMPLE_KEYS = ("D", "E")
ALIAS_DIR = DATA_DIR / "aliases"
ALIAS_RUN_DIR = DATA_DIR / "alias_runs"
MAX_ALIAS_RUNS_KEPT = 200
MAX_NEST_DEPTH = 8
