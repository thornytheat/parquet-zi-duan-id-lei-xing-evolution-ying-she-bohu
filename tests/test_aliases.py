"""路径别名：展平/核对/隔离（纯函数）+ 版本化发布（幂等/失败保留旧版）+ API 一致性。"""
import json

import pytest
from fastapi.testclient import TestClient

from app import alias_store, aliases, schemaio
from app.main import app
from app.schemaio import SchemaError

client = TestClient(app)


def sample(key):
    return schemaio.load_alias_sample(key)


def make_schema(table, fields):
    return schemaio.parse_schema({"table": table, "fields": fields}, "测试 schema")


# ---------- 纯函数：展平与核对 ----------

def test_flatten_paths_and_parent_chains():
    old = sample("D")["old"]
    entries = {e["path"]: e for e in aliases.flatten_schema(old)}
    assert "user.device.imei" in entries
    imei = entries["user.device.imei"]
    assert imei["field_id"] == 14
    assert imei["parent_chain"] == ["user", "device"]
    assert imei["parent_ids"] == [10, 13]
    assert imei["depth"] == 2
    assert entries["user.device"]["is_struct"] is True
    assert entries["dt"]["partition"] is True


def test_sample_d_validated_with_four_aliases():
    s = sample("D")
    plan = aliases.build_plan(s["old"], s["new"])
    assert plan["status"] == "validated"
    assert plan["conflicts"] == [] and plan["missing"] == []
    assert len(plan["aliases"]) == 4
    assert plan["rewrite_map"] == {
        "user.session_id": "context.session_id",
        "user.device": "context.device",
        "user.device.imei": "context.device.imei",
        "user.device.os": "context.device.os",
    }
    # 新查询按字段身份落到新路径
    assert plan["resolution_map"]["14"] == "context.device.imei"
    alias = next(a for a in plan["aliases"] if a["field_id"] == 14)
    assert alias["old_parent_chain"] == ["user", "device"]
    assert alias["new_parent_chain"] == ["context", "device"]
    # 新增父结构只标注，不参与匹配
    assert any(n["code"] == "added_in_new" and n["field"] == "context" for n in plan["notes"])


def test_sample_e_quarantined_and_never_reads_similar_columns():
    s = sample("E")
    plan = aliases.build_plan(s["old"], s["new"])
    assert plan["status"] == "quarantined"
    # 整批隔离：候选再合法也不生效
    assert plan["aliases"] == []
    assert plan["rewrite_map"] == {} and plan["resolution_map"] == {}
    assert {c["code"] for c in plan["conflicts"]} == {"type_mismatch"}
    assert {m["code"] for m in plan["missing"]} == {"missing_in_new", "id_missing_vanished"}
    # 相近列（同名不同 id / 同名不同层）只被标注为新增，绝不顶替旧字段
    all_new_paths = {a["new_path"] for a in plan["candidates"]}
    assert "context.referrer" not in all_new_paths
    assert "context.debug_flag" not in all_new_paths
    assert any(n["field"] == "context.referrer" and n["code"] == "added_in_new" for n in plan["notes"])
    assert any(n["field"] == "context.debug_flag" and n["code"] == "added_unverified" for n in plan["notes"])
    # 本可生效的候选（device/os 搬层）也被整批搁置
    assert {a["field_id"] for a in plan["candidates"]} == {13, 15}


def test_type_mismatch_on_move_is_conflict():
    old = make_schema("t", [{"id": 1, "name": "a", "type": "struct", "fields": [
        {"id": 2, "name": "x", "type": "string"}]}])
    new = make_schema("t", [{"id": 3, "name": "b", "type": "struct", "fields": [
        {"id": 2, "name": "x", "type": "int64"}]}])
    plan = aliases.build_plan(old, new)
    assert plan["status"] == "quarantined"
    assert plan["conflicts"][0]["code"] == "type_mismatch"
    assert plan["aliases"] == []


def test_struct_leaf_kind_change_is_conflict():
    old = make_schema("t", [{"id": 1, "name": "a", "type": "struct", "fields": [
        {"id": 2, "name": "x", "type": "string"}]}])
    new = make_schema("t", [{"id": 1, "name": "a", "type": "string"}])
    plan = aliases.build_plan(old, new)
    assert plan["status"] == "quarantined"
    codes = {c["code"] for c in plan["conflicts"]}
    assert "kind_changed" in codes  # struct → 叶子
    assert {m["code"] for m in plan["missing"]} == {"missing_in_new"}  # 子字段 id 2 丢失


def test_partition_field_move_is_conflict():
    old = make_schema("t", [{"id": 1, "name": "dt", "type": "date", "partition": True}])
    new = make_schema("t", [{"id": 9, "name": "meta", "type": "struct", "fields": [
        {"id": 1, "name": "dt", "type": "date"}]}])
    plan = aliases.build_plan(old, new)
    assert plan["status"] == "quarantined"
    assert plan["conflicts"][0]["code"] == "partition_moved"


def test_parent_chain_check_defensive():
    # flatten 正常不会产出断裂的父级链；直接构造核对防御逻辑
    entry = {"path": "a.b.c", "parent_chain": ["a", "b"]}
    assert aliases._chain_ok(entry, {"a": {"is_struct": True}, "a.b": {"is_struct": True}}) is None
    assert "不存在" in aliases._chain_ok(entry, {"a": {"is_struct": True}})
    assert "不是 struct" in aliases._chain_ok(
        entry, {"a": {"is_struct": True}, "a.b": {"is_struct": False, "type": "string"}}
    )


def test_no_id_field_vanished_is_missing_not_guessed():
    old = make_schema("t", [{"name": "flag", "type": "string"}])
    new = make_schema("t", [{"id": 7, "name": "wrap", "type": "struct", "fields": [
        {"name": "flag", "type": "string"}]}])
    plan = aliases.build_plan(old, new)
    assert plan["status"] == "quarantined"
    assert plan["missing"][0]["code"] == "id_missing_vanished"
    assert plan["aliases"] == []  # 不按名称相近把旧值读到 wrap.flag


def test_fingerprint_stable_for_key_order():
    s = sample("D")
    shuffled_old = json.loads(json.dumps(s["old"], sort_keys=True))
    assert aliases.fingerprint(s["old"], s["new"]) == aliases.fingerprint(shuffled_old, s["new"])
    assert aliases.fingerprint(s["old"], s["new"]).startswith("sha256:")


@pytest.mark.parametrize("doc,needle", [
    ({"table": "t", "fields": [{"id": 1, "name": "a", "type": "struct"}]}, "fields 必须是非空数组"),
    ({"table": "t", "fields": [{"id": 1, "name": "a", "type": "string", "fields": []}]}, "不能带 fields"),
    ({"table": "t", "fields": [{"id": 1, "name": "a", "type": "struct", "fields": [
        {"id": 1, "name": "x", "type": "string"}]}]}, "field_id 重复"),
    ({"table": "t", "fields": [{"id": 1, "name": "a.b", "type": "string"}]}, "不能包含点号"),
    ({"table": "t", "fields": [{"id": 1, "name": "a", "type": "struct", "fields": [
        {"id": 2, "name": "x", "type": "date", "partition": True}]}]}, "分区字段必须是顶层字段"),
    ({"table": "t", "fields": [{"id": 1, "name": "a", "type": "struct", "fields": [
        {"id": 2, "name": "x", "type": "struct", "fields": [
            {"id": 3, "name": "y", "type": "string"}]}]}]}, None),  # 合法：三层嵌套
])
def test_nested_schema_parse_rules(doc, needle):
    if needle is None:
        parsed = schemaio.parse_schema(doc, "测试")
        assert parsed["fields"][0]["fields"][0]["fields"][0]["name"] == "y"
        return
    with pytest.raises(SchemaError) as exc:
        schemaio.parse_schema(doc, "测试")
    assert needle in str(exc.value)


# ---------- 版本化发布：幂等 / 回滚复用 / 失败保留旧版 ----------

def _plan_for(table, with_extra=False):
    """干净的搬层：x 从 struct a 内搬到顶层；with_extra 时两侧同时多一个未变列（改变指纹）。"""
    old_fields = [
        {"id": 1, "name": "a", "type": "struct", "fields": [
            {"id": 2, "name": "x", "type": "string"},
            {"id": 5, "name": "y", "type": "int32"}]},
    ]
    new_fields = [
        {"id": 1, "name": "a", "type": "struct", "fields": [
            {"id": 5, "name": "y", "type": "int32"}]},
        {"id": 2, "name": "x", "type": "string"},
    ]
    if with_extra:
        old_fields.append({"id": 4, "name": "c", "type": "int32"})
        new_fields.append({"id": 4, "name": "c", "type": "int32"})
    old = make_schema(table, old_fields)
    new = make_schema(table, new_fields)
    plan = aliases.build_plan(old, new)
    assert plan["status"] == "validated"
    return old, new, plan


def test_publish_idempotent_and_rollback_reuses_version():
    old, new, plan = _plan_for("t_pub")
    fp = aliases.fingerprint(old, new)
    r1 = alias_store.publish("t_pub", fp, plan, "alias-20261115T000000-aaaa01")
    assert r1["changed"] is True and r1["reused"] is False
    v1 = r1["version"]
    # 同一冻结输入重试：不新增版本
    r2 = alias_store.publish("t_pub", fp, plan, "alias-20261115T000001-aaaa02")
    assert r2["changed"] is False and r2["reused"] is True and r2["version"] == v1
    assert alias_store.list_versions("t_pub") == [v1]
    # 换一个冻结输入：新增版本
    old2, new2, plan2 = _plan_for("t_pub", with_extra=True)
    fp2 = aliases.fingerprint(old2, new2)
    r3 = alias_store.publish("t_pub", fp2, plan2, "alias-20261115T000002-aaaa03")
    assert r3["version"] != v1 and r3["reused"] is False
    assert alias_store.load_current("t_pub")["version"] == r3["version"]
    # 重新发布第一份冻结输入：切回旧版本号，不新增版本
    r4 = alias_store.publish("t_pub", fp, plan, "alias-20261115T000003-aaaa04")
    assert r4["rolled_back"] is True and r4["version"] == v1
    assert alias_store.list_versions("t_pub") == [v1, r3["version"]]
    assert alias_store.load_current("t_pub")["fingerprint"] == fp


def test_publish_failure_keeps_old_mapping_and_retry_adds_no_version(monkeypatch):
    old, new, plan = _plan_for("t_fail")
    fp = aliases.fingerprint(old, new)
    r1 = alias_store.publish("t_fail", fp, plan, "alias-20261115T000010-bbbb01")
    v1 = r1["version"]

    old2, new2, plan2 = _plan_for("t_fail", with_extra=True)
    fp2 = aliases.fingerprint(old2, new2)

    real_write = alias_store._atomic_write

    def boom(path, text):
        if path.name == "current.json":
            raise OSError("模拟写盘失败")
        return real_write(path, text)

    monkeypatch.setattr(alias_store, "_atomic_write", boom)
    with pytest.raises(alias_store.AliasStoreError):
        alias_store.publish("t_fail", fp2, plan2, "alias-20261115T000011-bbbb02")
    monkeypatch.undo()
    # 发布失败：旧版映射保持生效
    assert alias_store.load_current("t_fail")["version"] == v1
    # 重试同一冻结输入：复用已写出的版本文件，不新增版本
    r2 = alias_store.publish("t_fail", fp2, plan2, "alias-20261115T000012-bbbb03")
    assert r2["reused"] is True
    assert len(alias_store.list_versions("t_fail")) == 2
    assert alias_store.load_current("t_fail")["fingerprint"] == fp2


def test_quarantined_plan_cannot_be_published():
    s = sample("E")
    plan = aliases.build_plan(s["old"], s["new"])
    with pytest.raises(alias_store.AliasStoreError):
        alias_store.publish("dwd_events", aliases.fingerprint(s["old"], s["new"]), plan, "alias-20261115T000020-cccc01")


# ---------- API：方案 → 发布 → 历史/下载/当前映射一致 ----------

def test_aliases_page_renders():
    res = client.get("/aliases?sample=D")
    assert res.status_code == 200
    assert "路径别名工作台" in res.text and "dwd_events" in res.text


def test_plan_publish_and_consistency_across_views():
    # 生成方案（样例 D）
    res = client.post("/api/aliases/plan", json={"sample": "D"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "planned"
    run_id = body["run_id"]
    page = client.get(body["redirect"])
    assert page.status_code == 200 and "别名明细" in page.text

    # 发布
    pub = client.post("/api/aliases/publish", json={"run_id": run_id})
    assert pub.status_code == 200
    version = pub.json()["publish"]["version"]

    # 同一 run 重复发布：幂等
    pub2 = client.post("/api/aliases/publish", json={"run_id": run_id})
    assert pub2.json()["publish"]["version"] == version

    # 同一冻结输入再走一遍「生成→发布」：复用同一版本，不新增
    again = client.post("/api/aliases/plan", json={"sample": "D"}).json()
    pub3 = client.post("/api/aliases/publish", json={"run_id": again["run_id"]})
    assert pub3.json()["publish"]["version"] == version
    assert pub3.json()["publish"]["reused"] is True

    # 当前生效映射 = 本次发布结果
    current = client.get("/api/aliases/current?table=dwd_events").json()
    assert current["version"] == version
    assert len(current["aliases"]) == 4
    assert current["rewrite_map"]["user.device.imei"] == "context.device.imei"

    # 历史记录里的同一条运行
    runs = client.get("/api/aliases/runs").json()["runs"]
    row = next(r for r in runs if r["run_id"] == run_id)
    assert row["status"] == "published" and row["summary"]["version"] == version

    # 下载内容与运行记录、当前生效映射三方一致（同一次生效结果）
    run_doc = client.get(f"/api/aliases/runs/{run_id}").json()
    downloaded = client.get(f"/api/aliases/runs/{run_id}/alias.json").json()
    assert downloaded["run_id"] == run_id
    assert downloaded["published_version"] == version
    assert downloaded["aliases"] == run_doc["plan"]["aliases"] == current["aliases"]
    assert downloaded["rewrite_map"] == current["rewrite_map"]

    sql = client.get(f"/api/aliases/runs/{run_id}/rewrite.sql")
    assert sql.status_code == 200
    assert "CREATE OR REPLACE VIEW" in sql.text
    assert "`context`.`device`.`imei` AS `user.device.imei`" in sql.text
    assert "attachment" in sql.headers.get("content-disposition", "")


def test_quarantined_batch_cannot_publish_and_current_untouched():
    before = client.get("/api/aliases/current?table=dwd_events")
    res = client.post("/api/aliases/plan", json={"sample": "E"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "quarantined"
    assert body["summary"]["conflicts"] == 1 and body["summary"]["missing"] == 3

    pub = client.post("/api/aliases/publish", json={"run_id": body["run_id"]})
    assert pub.status_code == 409
    assert "整批隔离" in pub.json()["detail"]

    # 生效映射不因隔离批次改变
    after = client.get("/api/aliases/current?table=dwd_events")
    assert after.status_code == before.status_code
    if before.status_code == 200:
        assert after.json()["version"] == before.json()["version"]

    # 隔离方案的下载产物不含任何生效别名
    run_id = body["run_id"]
    downloaded = client.get(f"/api/aliases/runs/{run_id}/alias.json").json()
    assert downloaded["status"] == "quarantined"
    assert downloaded["aliases"] == [] and downloaded["rewrite_map"] == {}
    sql = client.get(f"/api/aliases/runs/{run_id}/rewrite.sql").text
    assert "CREATE OR REPLACE VIEW" not in sql
    assert "整批隔离" in sql


def test_alias_bad_inputs():
    res = client.post("/api/aliases/plan", json={"old": "{bad", "new": "{}"})
    assert res.status_code == 400
    res = client.post("/api/aliases/plan", json={
        "old": {"table": "t1", "fields": [{"id": 1, "name": "a", "type": "string"}]},
        "new": {"table": "t2", "fields": [{"id": 1, "name": "a", "type": "string"}]},
    })
    assert res.status_code == 400 and "表名不一致" in res.json()["detail"]
    assert client.post("/api/aliases/publish", json={"run_id": "alias-20990101T000000-abcdef"}).status_code == 404
    assert client.get("/api/aliases/runs/alias-20990101T000000-abcdef").status_code == 404
    assert client.get("/api/aliases/current?table=no_such_table").status_code == 404
    assert client.get("/api/aliases/samples/Z").status_code == 404
