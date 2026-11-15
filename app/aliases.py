"""路径别名（纯函数）：字段在结构层级间搬迁后，旧路径 → 新路径 的映射方案。

背景：上游把事件表字段从一层 struct 搬到另一层，同一份 Parquet 只存新路径；
旧报表仍按原路径取值，新查询按字段身份（field_id）落到新路径，因此需要一层
「旧路径 → 新路径」的别名映射，而不是为两边各存一份数据。

核对口径（与需求逐条对应）：
- 编号：字段身份只认 field_id，同一 id 在两侧各出现一次且路径不同才是搬迁候选；
  没有 id 的字段不参与搬迁判定。
- 类型：搬迁前后类型必须完全一致（别名只是改路径，不做任何类型转换）。
- 父级链：新路径的祖先逐段必须存在于新 schema 且都是 struct。
- 冲突或缺失 → 整批隔离（quarantined）：不产出任何生效别名，
  也绝不按名称相近把旧值猜到别的列上（相近列只作为“新增”标注，不做匹配）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

STATUS_LABELS = {"validated": "校验通过", "quarantined": "整批隔离"}
RUN_STATUS_LABELS = {
    "planned": "待发布",
    "quarantined": "已隔离",
    "published": "已生效",
    "publish_failed": "发布失败",
}

CONFLICT_LABELS = {
    "type_mismatch": "类型不一致",
    "kind_changed": "结构/叶子互变",
    "parent_chain_broken": "父级链不完整",
    "alias_collision": "新路径冲突",
    "partition_moved": "分区字段搬迁",
}
MISSING_LABELS = {
    "missing_in_new": "新 schema 缺失",
    "id_missing_vanished": "无 id 且路径消失",
}
NOTE_LABELS = {
    "added_in_new": "新 schema 新增",
    "added_unverified": "新增（无 id 未核对）",
    "unchanged_unverified": "未变（无 id 未核对）",
    "type_changed_in_place": "原地类型变更",
}


def flatten_schema(schema: dict) -> list[dict]:
    """把嵌套 schema 展平成路径条目；每个条目带父级链（名称与 field_id）。"""
    entries: list[dict] = []

    def walk(fields: list[dict], chain_names: list[str], chain_ids: list) -> None:
        for f in fields:
            path = ".".join(chain_names + [f["name"]])
            entries.append(
                {
                    "path": path,
                    "name": f["name"],
                    "field_id": f.get("id"),
                    "type": f["type"],
                    "is_struct": f["type"] == "struct",
                    "nullable": bool(f.get("nullable", True)),
                    "partition": bool(f.get("partition")),
                    "parent_chain": list(chain_names),
                    "parent_ids": list(chain_ids),
                    "depth": len(chain_names),
                }
            )
            if f["type"] == "struct":
                walk(f["fields"], chain_names + [f["name"]], chain_ids + [f.get("id")])

    walk(schema["fields"], [], [])
    return entries


def fingerprint(old: dict, new: dict) -> str:
    """冻结输入指纹：解析后的规范化 schema 做规范 JSON 序列化再取 sha256。"""
    canonical = json.dumps(
        {"old": old, "new": new}, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _chain_ok(entry: dict, new_by_path: dict) -> str | None:
    """父级链核对：新路径的每段祖先都必须在新 schema 中存在且为 struct。"""
    for depth in range(1, len(entry["parent_chain"]) + 1):
        ancestor_path = ".".join(entry["parent_chain"][:depth])
        ancestor = new_by_path.get(ancestor_path)
        if ancestor is None:
            return f"新路径 {entry['path']} 的父级 {ancestor_path} 在新 schema 中不存在"
        if not ancestor["is_struct"]:
            return f"新路径 {entry['path']} 的父级 {ancestor_path} 不是 struct（实际 {ancestor['type']}）"
    return None


def build_plan(old: dict, new: dict) -> dict:
    """核对两版 schema，产出生效别名或整批隔离结论。纯函数，不读写文件。"""
    old_entries = flatten_schema(old)
    new_entries = flatten_schema(new)
    new_by_id: dict[int, dict] = {}
    for e in new_entries:
        if e["field_id"] is not None:
            new_by_id.setdefault(int(e["field_id"]), e)  # parse 已保证唯一，setdefault 仅防御
    new_by_path = {e["path"]: e for e in new_entries}
    old_paths = {e["path"] for e in old_entries}

    candidates: list[dict] = []
    conflicts: list[dict] = []
    missing: list[dict] = []
    notes: list[dict] = []
    unchanged = 0
    claimed_new_paths: set[str] = set()

    for e in old_entries:
        fid = e["field_id"]

        # 无 id：无法核对身份。路径还在 → 容忍并标注；路径消失 → 缺失，整批隔离。
        if fid is None:
            same = new_by_path.get(e["path"])
            if same is None:
                missing.append(
                    {
                        "code": "id_missing_vanished",
                        "code_label": MISSING_LABELS["id_missing_vanished"],
                        "field": e["path"],
                        "old_path": e["path"],
                        "new_path": None,
                        "text": f"旧字段 {e['path']} 没有 field_id 且路径在新 schema 中消失："
                                f"无法核对身份，整批隔离；不按名称相近猜测归属",
                    }
                )
            elif same["type"] != e["type"]:
                notes.append(
                    {
                        "code": "type_changed_in_place",
                        "code_label": NOTE_LABELS["type_changed_in_place"],
                        "field": e["path"],
                        "text": f"{e['path']} 无 field_id，路径未变但类型 {e['type']}→{same['type']}："
                                f"属演进对比范畴，别名不处理",
                    }
                )
            else:
                unchanged += 1
                notes.append(
                    {
                        "code": "unchanged_unverified",
                        "code_label": NOTE_LABELS["unchanged_unverified"],
                        "field": e["path"],
                        "text": f"{e['path']} 无 field_id，路径与类型均未变（未核对身份）",
                    }
                )
            continue

        n = new_by_id.get(int(fid))
        if n is None:
            missing.append(
                {
                    "code": "missing_in_new",
                    "code_label": MISSING_LABELS["missing_in_new"],
                    "field": e["path"],
                    "old_path": e["path"],
                    "new_path": None,
                    "text": f"field_id {fid}（{e['path']}）在新 schema 中不存在："
                            f"无法建立别名，整批隔离",
                }
            )
            continue

        if n["path"] == e["path"]:
            if n["is_struct"] != e["is_struct"]:
                conflicts.append(
                    {
                        "code": "kind_changed",
                        "code_label": CONFLICT_LABELS["kind_changed"],
                        "field": e["path"],
                        "old_path": e["path"],
                        "new_path": n["path"],
                        "text": f"field_id {fid}（{e['path']}）由 "
                                f"{'struct' if e['is_struct'] else '叶子'} 变为 "
                                f"{'struct' if n['is_struct'] else '叶子'}：结构性质改变，不能作为别名",
                    }
                )
            elif n["type"] != e["type"]:
                notes.append(
                    {
                        "code": "type_changed_in_place",
                        "code_label": NOTE_LABELS["type_changed_in_place"],
                        "field": e["path"],
                        "text": f"field_id {fid}（{e['path']}）路径未变，类型 {e['type']}→{n['type']}："
                                f"属演进对比范畴，别名不处理",
                    }
                )
            else:
                unchanged += 1
            continue

        # 路径不同 → 搬迁候选，逐项核对：分区 / 结构性质 / 类型 / 父级链 / 新路径唯一
        if e["partition"] or n["partition"]:
            conflicts.append(
                {
                    "code": "partition_moved",
                    "code_label": CONFLICT_LABELS["partition_moved"],
                    "field": e["path"],
                    "old_path": e["path"],
                    "new_path": n["path"],
                    "text": f"field_id {fid} 是分区字段，跨层搬迁会使历史分区路径失配，不能作为别名",
                }
            )
            continue
        if n["is_struct"] != e["is_struct"]:
            conflicts.append(
                {
                    "code": "kind_changed",
                    "code_label": CONFLICT_LABELS["kind_changed"],
                    "field": e["path"],
                    "old_path": e["path"],
                    "new_path": n["path"],
                    "text": f"field_id {fid} 由 {'struct' if e['is_struct'] else '叶子'} 变为 "
                            f"{'struct' if n['is_struct'] else '叶子'} 并搬迁到 {n['path']}：不能作为别名",
                }
            )
            continue
        if n["type"] != e["type"]:
            conflicts.append(
                {
                    "code": "type_mismatch",
                    "code_label": CONFLICT_LABELS["type_mismatch"],
                    "field": e["path"],
                    "old_path": e["path"],
                    "new_path": n["path"],
                    "text": f"field_id {fid} 搬迁前后类型不一致（{e['type']} → {n['type']}）："
                            f"别名只做路径映射、不做类型转换",
                }
            )
            continue
        chain_error = _chain_ok(n, new_by_path)
        if chain_error:
            conflicts.append(
                {
                    "code": "parent_chain_broken",
                    "code_label": CONFLICT_LABELS["parent_chain_broken"],
                    "field": e["path"],
                    "old_path": e["path"],
                    "new_path": n["path"],
                    "text": chain_error,
                }
            )
            continue
        if n["path"] in claimed_new_paths:
            conflicts.append(
                {
                    "code": "alias_collision",
                    "code_label": CONFLICT_LABELS["alias_collision"],
                    "field": e["path"],
                    "old_path": e["path"],
                    "new_path": n["path"],
                    "text": f"新路径 {n['path']} 已被另一条别名占用",
                }
            )
            continue
        claimed_new_paths.add(n["path"])
        candidates.append(
            {
                "field_id": int(fid),
                "name": n["name"],
                "type": n["type"],
                "old_path": e["path"],
                "new_path": n["path"],
                "old_parent_chain": e["parent_chain"],
                "new_parent_chain": n["parent_chain"],
                "old_parent_ids": e["parent_ids"],
                "new_parent_ids": n["parent_ids"],
                "is_struct": e["is_struct"],
                "depth_old": e["depth"],
                "depth_new": n["depth"],
            }
        )

    # 新 schema 独有的条目：只标注，不参与匹配（相近列绝不顶替旧字段）
    old_ids = {int(e["field_id"]) for e in old_entries if e["field_id"] is not None}
    added = 0
    for n in new_entries:
        if n["field_id"] is not None:
            if int(n["field_id"]) not in old_ids:
                added += 1
                notes.append(
                    {
                        "code": "added_in_new",
                        "code_label": NOTE_LABELS["added_in_new"],
                        "field": n["path"],
                        "text": f"field_id {n['field_id']}（{n['path']}）是新 schema 新增条目，旧报表不引用",
                    }
                )
        elif n["path"] not in old_paths:
            added += 1
            notes.append(
                {
                    "code": "added_unverified",
                    "code_label": NOTE_LABELS["added_unverified"],
                    "field": n["path"],
                    "text": f"{n['path']} 是新 schema 新增条目（无 field_id，未核对身份）",
                }
            )

    status = "quarantined" if (conflicts or missing) else "validated"
    # 整批隔离：候选全部不生效，生效别名为空
    effective = candidates if status == "validated" else []
    counts = {
        "old_entries": len(old_entries),
        "new_entries": len(new_entries),
        "moved": len(candidates),
        "unchanged": unchanged,
        "added": added,
        "conflicts": len(conflicts),
        "missing": len(missing),
    }
    return {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "aliases": effective,
        "candidates": candidates,
        "rewrite_map": {a["old_path"]: a["new_path"] for a in effective},
        "resolution_map": {str(a["field_id"]): a["new_path"] for a in effective},
        "conflicts": conflicts,
        "missing": missing,
        "notes": notes,
        "counts": counts,
    }


def _quote_path(path: str) -> str:
    return ".".join("`" + seg.replace("`", "``") + "`" for seg in path.split("."))


def _rewrite_sql(table: str, fp: str, plan: dict) -> str:
    lines = [
        f"-- schemaevo 路径别名 · 表 {table}",
        f"-- 冻结输入指纹 {fp}",
    ]
    if plan["status"] == "quarantined":
        lines.append("-- 状态：整批隔离（quarantined）。存在冲突/缺失，不产出任何生效别名；")
        lines.append("-- 旧值不会被读到相近列。消除以下问题后重新生成方案：")
        for c in plan["conflicts"]:
            lines.append(f"-- 冲突 [{c['code']}] {c['text']}")
        for m in plan["missing"]:
            lines.append(f"-- 缺失 [{m['code']}] {m['text']}")
        return "\n".join(lines) + "\n"
    if not plan["aliases"]:
        lines.append("-- 两版 schema 之间没有路径搬迁，无需别名视图。")
        return "\n".join(lines) + "\n"
    lines.append("-- 旧报表继续按原路径取值；本视图把旧路径映射到搬层后的新路径，")
    lines.append("-- 同一份 Parquet 只存新路径，不为新旧两边各存一份。")
    lines.append(f"CREATE OR REPLACE VIEW `{table}__legacy_paths` AS")
    lines.append("SELECT")
    last = len(plan["aliases"]) - 1
    for i, a in enumerate(plan["aliases"]):
        comma = "," if i < last else ""
        lines.append(
            f"  {_quote_path(a['new_path'])} AS `{a['old_path'].replace('`', '``')}`{comma}"
            f"  -- field_id {a['field_id']} · {a['type']}"
        )
    lines.append(f"FROM `{table}`;")
    return "\n".join(lines) + "\n"


def build_artifacts(table: str, fp: str, plan: dict) -> dict:
    """下载产物在生成方案时一次算好并随运行记录保存，保证下载内容=复核内容。"""
    alias_doc = {
        "table": table,
        "fingerprint": fp,
        "status": plan["status"],
        "status_label": plan["status_label"],
        "aliases": plan["aliases"],
        "rewrite_map": plan["rewrite_map"],
        "resolution_map": plan["resolution_map"],
        "counts": plan["counts"],
        "quarantine": (
            {"conflicts": plan["conflicts"], "missing": plan["missing"]}
            if plan["status"] == "quarantined"
            else None
        ),
    }
    return {"alias_json": alias_doc, "rewrite_sql": _rewrite_sql(table, fp, plan)}


def build_run(old: dict, new: dict, source: str = "自定义输入") -> dict:
    """核心编排：纯计算，不读写文件；run_id 由存储层分配。"""
    plan = build_plan(old, new)
    fp = fingerprint(old, new)
    status = "planned" if plan["status"] == "validated" else "quarantined"
    return {
        "run_id": None,
        "kind": "path_alias",
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "source": source,
        "table": new["table"],
        "fingerprint": fp,
        "status": status,
        "status_label": RUN_STATUS_LABELS[status],
        "input": {"old": old, "new": new},
        "plan": plan,
        "artifacts": build_artifacts(new["table"], fp, plan),
        "publish": None,
        "summary": {
            "status": status,
            "status_label": RUN_STATUS_LABELS[status],
            "aliases": len(plan["aliases"]),
            "candidates": len(plan["candidates"]),
            "conflicts": plan["counts"]["conflicts"],
            "missing": plan["counts"]["missing"],
            "version": None,
        },
    }
