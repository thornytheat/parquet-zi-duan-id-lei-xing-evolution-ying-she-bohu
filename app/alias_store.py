"""路径别名的版本化发布与运行记录持久化。

发布语义（与需求逐条对应）：
- 原子生效：先写版本文件 → 再更新索引 → 最后才切换 current 指针；
  任何一步失败，current 仍指向旧版映射（发布失败保留旧版映射）。
- 幂等：同一冻结输入（指纹相同）重试不新增版本——
  与 current 相同 → 直接复用；与历史版本相同 → 只把 current 切回该版本（回滚也不新增版本）。
- 整批隔离的方案不允许发布。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import settings

ALIAS_RUN_ID_RE = re.compile(r"^alias-\d{8}T\d{6}-[0-9a-f]{6}$")
TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AliasStoreError(RuntimeError):
    """发布失败；current 指针未被移动，旧版映射保持生效。"""


def new_alias_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"alias-{stamp}-{uuid.uuid4().hex[:6]}"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


# ---------- 运行记录（方案 + 发布结果，历史/下载共用同一份） ----------

def _run_path(run_id: str) -> Path:
    if not ALIAS_RUN_ID_RE.match(run_id or ""):
        raise KeyError(f"别名运行记录 {run_id!r} 不存在")
    return settings.ALIAS_RUN_DIR / f"{run_id}.json"


def save_alias_run(run: dict) -> dict:
    run = dict(run)
    run["run_id"] = run.get("run_id") or new_alias_run_id()
    _atomic_write(_run_path(run["run_id"]), json.dumps(run, ensure_ascii=False, indent=2))
    _prune_runs()
    return run


def load_alias_run(run_id: str) -> dict:
    path = _run_path(run_id)
    if not path.exists():
        raise KeyError(f"别名运行记录 {run_id!r} 不存在")
    return json.loads(path.read_text(encoding="utf-8"))


def list_alias_runs(limit: int = 50) -> list[dict]:
    settings.ALIAS_RUN_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        settings.ALIAS_RUN_DIR.glob("alias-*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    rows = []
    for path in files[:limit]:
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        rows.append(
            {
                "run_id": run.get("run_id"),
                "table": run.get("table"),
                "created_at": run.get("created_at"),
                "source": run.get("source"),
                "status": run.get("status"),
                "status_label": run.get("status_label"),
                "summary": run.get("summary", {}),
            }
        )
    return rows


def _prune_runs() -> None:
    files = sorted(
        settings.ALIAS_RUN_DIR.glob("alias-*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for path in files[settings.MAX_ALIAS_RUNS_KEPT:]:
        try:
            path.unlink()
        except OSError:
            pass


# ---------- 版本化映射（每表一条 current 指针 + 指纹索引的版本库） ----------

def _table_dir(table: str) -> Path:
    if not TABLE_RE.match(table or ""):
        raise AliasStoreError(f"表名 {table!r} 不安全，无法作为别名目录")
    return settings.ALIAS_DIR / table


def load_current(table: str) -> dict | None:
    return _read_json(_table_dir(table) / "current.json")


def list_versions(table: str) -> list[str]:
    index = _read_json(_table_dir(table) / "versions" / "index.json")
    return list(index.get("versions", [])) if index else []


def publish(table: str, fp: str, plan: dict, run_id: str) -> dict:
    """把校验通过的方案发布为生效映射；失败时 current 保持不动。"""
    if plan.get("status") != "validated":
        raise AliasStoreError("方案未通过校验（整批隔离中），不能发布")
    tdir = _table_dir(table)
    versions_dir = tdir / "versions"
    index_path = versions_dir / "index.json"
    current_path = tdir / "current.json"
    index = _read_json(index_path) or {"versions": [], "fingerprints": {}}
    current = _read_json(current_path)

    # 同一冻结输入重试：不新增版本
    if current and current.get("fingerprint") == fp:
        return {
            "changed": False,
            "reused": True,
            "rolled_back": False,
            "version": current["version"],
            "published_at": current["published_at"],
        }

    # 同一输入历史上发布过（含上次发布在切指针前失败）：复用版本号，只切指针
    known = index.get("fingerprints", {}).get(fp)
    if known:
        doc = _read_json(versions_dir / f"{known}.json")
        if doc is None:
            raise AliasStoreError(f"版本文件 {known} 缺失，版本索引损坏")
        try:
            _atomic_write(current_path, json.dumps(doc, ensure_ascii=False, indent=2))
        except OSError as exc:
            raise AliasStoreError(f"切换生效版本失败：{exc}；旧版映射保持生效") from exc
        return {
            "changed": True,
            "reused": True,
            "rolled_back": True,
            "version": known,
            "published_at": doc["published_at"],
        }

    version = f"v{len(index.get('versions', [])) + 1:04d}"
    doc = {
        "version": version,
        "table": table,
        "fingerprint": fp,
        "published_at": _now(),
        "source_run_id": run_id,
        "aliases": plan["aliases"],
        "rewrite_map": plan["rewrite_map"],
        "resolution_map": plan["resolution_map"],
        "counts": plan["counts"],
    }
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    try:
        _atomic_write(versions_dir / f"{version}.json", text)  # 1) 先落版本文件
        index.setdefault("versions", []).append(version)
        index.setdefault("fingerprints", {})[fp] = version
        _atomic_write(index_path, json.dumps(index, ensure_ascii=False, indent=2))  # 2) 再更新索引
        _atomic_write(current_path, text)  # 3) 最后才切换 current；失败则旧版继续生效
    except OSError as exc:
        raise AliasStoreError(f"发布失败：{exc}；旧版映射保持生效") from exc
    return {
        "changed": True,
        "reused": False,
        "rolled_back": False,
        "version": version,
        "published_at": doc["published_at"],
    }
