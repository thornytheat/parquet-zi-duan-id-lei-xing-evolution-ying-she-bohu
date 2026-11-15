"""schema / 数据文件清单的解析、校验与样例加载。"""
from __future__ import annotations

import json
from pathlib import Path

from . import settings
from .types import TypeParseError, normalize_type


class SchemaError(ValueError):
    """输入不合法；路由层统一转 400。"""


def _load_json(value, label: str):
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise SchemaError(f"{label} 为空，请粘贴 JSON 或选择样例")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SchemaError(f"{label} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}")
    return value


def _parse_fields(raw_fields: list, label: str, prefix: str, seen_ids: set[int], depth: int) -> list[dict]:
    """解析一层字段；struct 递归向下。field_id 全表唯一，字段名同级唯一。"""
    if depth > settings.MAX_NEST_DEPTH:
        raise SchemaError(f"{label} 嵌套层级超过 {settings.MAX_NEST_DEPTH} 层：{prefix}")
    fields: list[dict] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(raw_fields, start=1):
        where = f"{label} 第 {index} 个字段" if not prefix else f"{label} 字段 {prefix} 下第 {index} 个子字段"
        if not isinstance(raw, dict):
            raise SchemaError(f"{where}必须是 JSON 对象")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise SchemaError(f"{where}缺少 name")
        if "." in name:
            raise SchemaError(f"{label} 字段名 {name!r} 不能包含点号（点号是路径分隔符）")
        if name in seen_names:
            raise SchemaError(f"{label} 字段名重复：{name}")
        seen_names.add(name)
        fid = raw.get("id")
        if fid is not None:
            try:
                fid = int(fid)
            except (TypeError, ValueError):
                raise SchemaError(f"{label} 字段 {name} 的 id 必须是整数")
            if fid in seen_ids:
                raise SchemaError(f"{label} field_id 重复：{fid}")
            seen_ids.add(fid)
        partition = bool(raw.get("partition", False))
        type_text = str(raw.get("type") or "").strip().lower()
        if type_text == "struct":
            children = raw.get("fields")
            if not isinstance(children, list) or not children:
                raise SchemaError(f"{label} struct 字段 {name} 的 fields 必须是非空数组")
            if partition:
                raise SchemaError(f"{label} struct 字段 {name} 不能作为分区字段")
            sub = _parse_fields(children, label, f"{prefix}.{name}" if prefix else name, seen_ids, depth + 1)
            fields.append(
                {
                    "id": fid,
                    "name": name,
                    "type": "struct",
                    "nullable": bool(raw.get("nullable", True)),
                    "default": raw.get("default"),
                    "partition": False,
                    "fields": sub,
                }
            )
            continue
        if raw.get("fields") is not None:
            raise SchemaError(f"{label} 字段 {name} 不是 struct，不能带 fields")
        try:
            ftype = normalize_type(str(raw.get("type") or ""))
        except TypeParseError as exc:
            raise SchemaError(f"{label} 字段 {name} 类型不合法：{exc}")
        if prefix and partition:
            raise SchemaError(f"{label} 分区字段必须是顶层字段：{prefix}.{name}")
        fields.append(
            {
                "id": fid,
                "name": name,
                "type": ftype,
                "nullable": bool(raw.get("nullable", True)),
                "default": raw.get("default"),
                "partition": partition,
            }
        )
    return fields


def parse_schema(value, label: str = "schema") -> dict:
    doc = _load_json(value, label)
    if not isinstance(doc, dict):
        raise SchemaError(f"{label} 必须是 JSON 对象")
    table = str(doc.get("table") or "").strip()
    if not table:
        raise SchemaError(f"{label} 缺少 table 名称")
    raw_fields = doc.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise SchemaError(f"{label} 的 fields 必须是非空数组")
    fields = _parse_fields(raw_fields, label, prefix="", seen_ids=set(), depth=1)
    partitions = [f["name"] for f in fields if f["partition"]]
    return {"table": table, "fields": fields, "partitions": partitions}


def parse_consumers(value, label: str = "consumers") -> dict:
    doc = _load_json(value, label)
    if not isinstance(doc, dict):
        raise SchemaError(f"{label} 必须是 JSON 对象")
    raw_files = doc.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise SchemaError(f"{label} 的 files 必须是非空数组")
    files: list[dict] = []
    for index, raw in enumerate(raw_files, start=1):
        if not isinstance(raw, dict):
            raise SchemaError(f"{label} 第 {index} 个文件必须是 JSON 对象")
        path = str(raw.get("path") or "").strip()
        if not path:
            raise SchemaError(f"{label} 第 {index} 个文件缺少 path")
        columns = raw.get("columns")
        if not isinstance(columns, list) or not columns:
            raise SchemaError(f"{label} 文件 {path} 的 columns 必须是非空数组")
        cleaned = [str(c).strip() for c in columns if str(c).strip()]
        if not cleaned:
            raise SchemaError(f"{label} 文件 {path} 的 columns 不能全为空")
        files.append({"path": path, "columns": cleaned})
    return {"table": (doc.get("table") or "").strip() or None, "files": files}


def load_sample_index() -> list[dict]:
    index_path = settings.SAMPLE_DIR / "index.json"
    if not index_path.exists():
        raise SchemaError(f"样例索引缺失：{index_path}")
    doc = json.loads(index_path.read_text(encoding="utf-8"))
    return doc.get("samples", [])


def load_sample(key: str) -> dict:
    key = (key or "").strip().upper()
    entry = None
    for item in load_sample_index():
        if item.get("key") == key:
            entry = item
            break
    if entry is None:
        raise SchemaError(f"未知样例 {key!r}，可选：A / B / C")

    def read(rel: str) -> str:
        path = settings.SAMPLE_DIR / rel
        if not path.exists():
            raise SchemaError(f"样例文件缺失：{rel}")
        return path.read_text(encoding="utf-8")

    return {
        "key": key,
        "label": entry.get("label", key),
        "description": entry.get("description", ""),
        "expected": entry.get("expected", ""),
        "old": parse_schema(read(entry["old"]), f"样例 {key} 旧 schema"),
        "new": parse_schema(read(entry["new"]), f"样例 {key} 新 schema"),
        "consumers": parse_consumers(read(entry["consumers"]), f"样例 {key} 数据文件清单"),
        "raw_old": read(entry["old"]),
        "raw_new": read(entry["new"]),
        "raw_consumers": read(entry["consumers"]),
    }


def load_alias_sample_index() -> list[dict]:
    index_path = settings.ALIAS_SAMPLE_DIR / "index.json"
    if not index_path.exists():
        raise SchemaError(f"别名样例索引缺失：{index_path}")
    doc = json.loads(index_path.read_text(encoding="utf-8"))
    return doc.get("samples", [])


def load_alias_sample(key: str) -> dict:
    """路径别名样例（结构搬层，嵌套 schema，无 consumers 清单）。"""
    key = (key or "").strip().upper()
    entry = None
    for item in load_alias_sample_index():
        if item.get("key") == key:
            entry = item
            break
    if entry is None:
        raise SchemaError(f"未知别名样例 {key!r}，可选：D / E")

    def read(rel: str) -> str:
        path = settings.ALIAS_SAMPLE_DIR / rel
        if not path.exists():
            raise SchemaError(f"样例文件缺失：alias/{rel}")
        return path.read_text(encoding="utf-8")

    return {
        "key": key,
        "label": entry.get("label", key),
        "description": entry.get("description", ""),
        "expected": entry.get("expected", ""),
        "old": parse_schema(read(entry["old"]), f"样例 {key} 旧 schema"),
        "new": parse_schema(read(entry["new"]), f"样例 {key} 新 schema"),
        "raw_old": read(entry["old"]),
        "raw_new": read(entry["new"]),
    }
