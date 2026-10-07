"""侧防引用实体资源（kind=section/switch）与实体共享冲突判断的回归测试。

覆盖：
* 所指区段被占用 / 被他进路直接锁用、所指道岔被他进路直接用 -> 即时申请红灯，
  且只增拒绝记录给出实体冲突依据（via_flank / held_as）；
* 反向：本进路持有侧防实体锁时，其他进路直接使用同一实体同样被阻止；
* 同一实体被多个侧防（不同侧防 ID）引用 -> 经实体互斥；
* 锁归属与释放时机：同一实体被本进路直接使用又被侧防引用时分别持锁、
  普通区段逐段释放不提前解除侧防保护；同一实体被多个侧防引用只写一把实体锁；
* 待行：实体冲突等待（不写假拒绝）、按实体争用决定越过、资源解除后兑现；
* generic 侧防仍独立互斥；缺号禁放行；故障持锁；复位/完成后释放；
* 重启后已持有的侧防锁继续有效。
"""

import json
import os
import tempfile

import pytest

from app import interlocking as il
from app.db import connect, init_db


# --------------------------------------------------------------------- #
# 公共夹具与站场
# --------------------------------------------------------------------- #
@pytest.fixture()
def conn():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    c = connect(path)
    yield c
    c.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def ev(seq, detector, kind):
    return {"seq": seq, "detector_id": detector, "kind": kind, "ts": 123}


def yard_flank():
    """四个互不相邻区段 + 侧防资源：

    FP-S3 / FP-S3B 均指向区段 S3（同一实体被多个侧防引用）；
    FP-GEN 为独立 generic 资源。
    """
    return {
        "yard_id": "flank-yard",
        "nodes": [{"id": n} for n in ("A", "B", "C", "D", "E", "F", "G", "H")],
        "sections": [
            {"id": "S1", "a": "A", "b": "B", "detector": "D1"},
            {"id": "S2", "a": "C", "b": "D", "detector": "D2"},
            {"id": "S3", "a": "E", "b": "F", "detector": "D3"},
            {"id": "S4", "a": "G", "b": "H", "detector": "D4"},
        ],
        "switches": [],
        "resources": [
            {"id": "FP-S3", "kind": "section", "ref": "S3"},
            {"id": "FP-S3B", "kind": "section", "ref": "S3"},
            {"id": "FP-GEN", "kind": "generic", "ref": "approach-protection"},
        ],
        "routes": [
            {"id": "R-USE3", "sections": ["S3"], "switches": {}, "flank": []},
            {"id": "R-FLANK", "sections": ["S1"], "switches": {}, "flank": ["FP-S3"]},
            {"id": "R-FLANK2", "sections": ["S2"], "switches": {}, "flank": ["FP-S3B"]},
            {"id": "R-MULTI", "sections": ["S1"], "switches": {}, "flank": ["FP-S3", "FP-S3B"]},
            {"id": "R-GEN", "sections": ["S4"], "switches": {}, "flank": ["FP-GEN"]},
            {"id": "R-GEN2", "sections": ["S3"], "switches": {}, "flank": ["FP-GEN"]},
        ],
    }


def yard_switch():
    """S1-S2 经道岔 SW1 相连；R-FLKSW 侧防引用 SW1 但不直接使用。"""
    return {
        "yard_id": "flank-switch-yard",
        "nodes": [{"id": n} for n in ("A", "B", "C", "D", "E")],
        "sections": [
            {"id": "S1", "a": "A", "b": "B", "detector": "D1"},
            {"id": "S2", "a": "B", "b": "C", "detector": "D2"},
            {"id": "S3", "a": "D", "b": "E", "detector": "D3"},
        ],
        "switches": [{"id": "SW1", "node": "B", "positions": ["straight", "diverging"]}],
        "resources": [{"id": "FP-SW1", "kind": "switch", "ref": "SW1"}],
        "routes": [
            {"id": "R-USE", "sections": ["S1", "S2"], "switches": {"SW1": "straight"}, "flank": []},
            {"id": "R-FLKSW", "sections": ["S3"], "switches": {}, "flank": ["FP-SW1"]},
        ],
    }


def yard_dual():
    """S1-S2-S3 直链；R-DUAL 直接使用 S2 又以 FP-S2 侧防引用同一实体。"""
    return {
        "yard_id": "dual-yard",
        "nodes": [{"id": n} for n in ("A", "B", "C", "D")],
        "sections": [
            {"id": "S1", "a": "A", "b": "B", "detector": "D1"},
            {"id": "S2", "a": "B", "b": "C", "detector": "D2"},
            {"id": "S3", "a": "C", "b": "D", "detector": "D3"},
        ],
        "switches": [],
        "resources": [{"id": "FP-S2", "kind": "section", "ref": "S2"}],
        "routes": [
            {"id": "R-DUAL", "sections": ["S1", "S2", "S3"], "switches": {}, "flank": ["FP-S2"]},
            {"id": "R-USE2", "sections": ["S2"], "switches": {}, "flank": []},
        ],
    }


def yard_chain_flank():
    """S1-S2 直链，R-F 持侧防 FP-S3（指向独立区段 S3），用于故障持锁测试。"""
    return {
        "yard_id": "chain-flank-yard",
        "nodes": [{"id": n} for n in ("A", "B", "C", "D", "E")],
        "sections": [
            {"id": "S1", "a": "A", "b": "B", "detector": "D1"},
            {"id": "S2", "a": "B", "b": "C", "detector": "D2"},
            {"id": "S3", "a": "D", "b": "E", "detector": "D3"},
        ],
        "switches": [],
        "resources": [{"id": "FP-S3", "kind": "section", "ref": "S3"}],
        "routes": [
            {"id": "R-F", "sections": ["S1", "S2"], "switches": {}, "flank": ["FP-S3"]},
            {"id": "R-USE3", "sections": ["S3"], "switches": {}, "flank": []},
        ],
    }


def _locks_of(conn, instance_id):
    snap = il.snapshot(conn)
    return [(l["resource_type"], l["resource_id"]) for l in snap["locks"] if l["instance_id"] == instance_id]


def _denied_approval_reasons(conn, route_id):
    row = conn.execute(
        "SELECT reasons FROM approvals WHERE route_id=? AND decision='denied' ORDER BY approval_id DESC LIMIT 1",
        (route_id,),
    ).fetchone()
    return json.loads(row["reasons"]) if row else []


# --------------------------------------------------------------------- #
# 即时申请：实体冲突红灯 + 冲突依据
# --------------------------------------------------------------------- #
def test_flank_section_occupied_blocks_immediate(conn):
    il.import_yard(conn, yard_flank())
    il.report_event(conn, ev(1, "D3", "occupy"))  # 外部占用侧防所指区段 S3
    r = il.request_route(conn, "R-FLANK")
    assert r["decision"] == "denied" and r["signal"] == "red"
    occ = [c for c in r["conflicts"] if c["type"] == "section_occupied"]
    assert occ and occ[0]["resource"] == "S3" and occ[0]["via_flank"] == "FP-S3"
    # 只增拒绝记录同样携带实体冲突依据
    reasons = _denied_approval_reasons(conn, "R-FLANK")
    assert any(x.get("via_flank") == "FP-S3" and x["resource"] == "S3" for x in reasons)


def test_flank_section_locked_by_direct_user_blocks(conn):
    il.import_yard(conn, yard_flank())
    il.request_route(conn, "R-USE3")  # 直接锁用 S3
    r = il.request_route(conn, "R-FLANK")
    assert r["decision"] == "denied"
    locked = [c for c in r["conflicts"] if c["type"] == "section_locked" and c["resource"] == "S3"]
    assert locked and locked[0]["via_flank"] == "FP-S3"
    assert locked[0]["held_by_route"] == "R-USE3" and locked[0]["held_as"] == "section"


def test_direct_use_blocked_by_flank_holder(conn):
    il.import_yard(conn, yard_flank())
    r1 = il.request_route(conn, "R-FLANK")
    assert r1["decision"] == "permitted"
    # 侧防实体锁已登记（flank_section）且与直接区段锁共享互斥
    held = _locks_of(conn, r1["instance_id"])
    assert ("flank_section", "S3") in held and ("flank", "FP-S3") in held
    r2 = il.request_route(conn, "R-USE3")
    assert r2["decision"] == "denied"
    locked = [c for c in r2["conflicts"] if c["type"] == "section_locked" and c["resource"] == "S3"]
    assert locked and locked[0]["held_as"] == "flank_section"
    assert locked[0]["held_by_route"] == "R-FLANK"


def test_same_entity_via_different_flank_ids_mutex(conn):
    il.import_yard(conn, yard_flank())
    assert il.request_route(conn, "R-FLANK")["decision"] == "permitted"   # 持 FP-S3 -> S3
    r = il.request_route(conn, "R-FLANK2")                                # 要 FP-S3B -> S3
    assert r["decision"] == "denied"
    locked = [c for c in r["conflicts"] if c["type"] == "section_locked" and c["resource"] == "S3"]
    assert locked and locked[0]["via_flank"] == "FP-S3B" and locked[0]["held_as"] == "flank_section"


def test_flank_switch_entity_shared(conn):
    # 直接使用道岔的进路阻止侧防引用该道岔的进路
    il.import_yard(conn, yard_switch())
    il.request_route(conn, "R-USE")
    r = il.request_route(conn, "R-FLKSW")
    assert r["decision"] == "denied"
    locked = [c for c in r["conflicts"] if c["type"] == "switch_locked" and c["resource"] == "SW1"]
    assert locked and locked[0]["via_flank"] == "FP-SW1" and locked[0]["held_as"] == "switch"


def test_direct_switch_use_blocked_by_flank_holder(conn):
    il.import_yard(conn, yard_switch())
    r1 = il.request_route(conn, "R-FLKSW")
    assert r1["decision"] == "permitted"
    assert ("flank_switch", "SW1") in _locks_of(conn, r1["instance_id"])
    r2 = il.request_route(conn, "R-USE")
    assert r2["decision"] == "denied"
    locked = [c for c in r2["conflicts"] if c["type"] == "switch_locked" and c["resource"] == "SW1"]
    assert locked and locked[0]["held_as"] == "flank_switch"


def test_generic_flank_stays_independent(conn):
    il.import_yard(conn, yard_flank())
    assert il.request_route(conn, "R-GEN")["decision"] == "permitted"   # 持 generic FP-GEN
    # generic 不触及任何实体：直接使用 S3 不受 FP-GEN 影响
    assert il.request_route(conn, "R-USE3")["decision"] == "permitted"


def test_generic_flank_mutex_by_flank_id(conn):
    il.import_yard(conn, yard_flank())
    il.request_route(conn, "R-GEN")
    r = il.request_route(conn, "R-GEN2")  # S3 空闲，仅 FP-GEN 被持
    assert r["decision"] == "denied"
    kinds = {c["type"] for c in r["conflicts"]}
    assert "flank_locked" in kinds and "section_locked" not in kinds


# --------------------------------------------------------------------- #
# 锁归属与释放时机
# --------------------------------------------------------------------- #
def test_multiple_flanks_same_entity_single_entity_lock(conn):
    il.import_yard(conn, yard_flank())
    r = il.request_route(conn, "R-MULTI")  # flank = [FP-S3, FP-S3B]，均指向 S3
    assert r["decision"] == "permitted"
    held = _locks_of(conn, r["instance_id"])
    # 同一实体只写一把侧防实体锁（避免重复写锁）；两个侧防 ID 锁各自登记
    assert held.count(("flank_section", "S3")) == 1
    assert ("flank", "FP-S3") in held and ("flank", "FP-S3B") in held


def test_dual_use_entity_lock_ownership_and_release_timing(conn):
    """同一实体被本进路直接使用又被侧防引用：

    * 直接锁（section）与侧防实体锁（flank_section）分别登记、各写一次；
    * 列车推进使普通区段锁逐段释放后，侧防实体锁仍保留（不提前失去侧防保护）；
    * 末段出清进路完成后，全部锁释放。
    """
    il.import_yard(conn, yard_dual())
    r = il.request_route(conn, "R-DUAL")
    assert r["decision"] == "permitted"
    iid = r["instance_id"]
    held = _locks_of(conn, iid)
    assert ("section", "S2") in held and ("flank_section", "S2") in held

    il.report_event(conn, ev(1, "D1", "occupy"))
    il.report_event(conn, ev(2, "D1", "clear"))
    il.report_event(conn, ev(3, "D2", "occupy"))
    il.report_event(conn, ev(4, "D2", "clear"))
    il.report_event(conn, ev(5, "D3", "occupy"))  # 推进到末段：S2 直接锁逐段释放
    held = _locks_of(conn, iid)
    assert ("section", "S2") not in held, "普通区段锁已按序逐段释放"
    assert ("flank_section", "S2") in held, "侧防实体锁必须保留到末段出清"
    # 此时其他进路仍不能直接使用 S2（侧防保护未提前失去）
    r2 = il.request_route(conn, "R-USE2")
    assert r2["decision"] == "denied"
    assert any(c["resource"] == "S2" and c["held_as"] == "flank_section" for c in r2["conflicts"])

    il.report_event(conn, ev(6, "D3", "clear"))  # 末段出清 -> 完成，全部锁释放
    assert _locks_of(conn, iid) == []
    assert il.request_route(conn, "R-USE2")["decision"] == "permitted"


def test_completion_releases_flank_entity_locks(conn):
    il.import_yard(conn, yard_flank())
    r = il.request_route(conn, "R-FLANK")
    iid = r["instance_id"]
    il.report_event(conn, ev(1, "D1", "occupy"))
    il.report_event(conn, ev(2, "D1", "clear"))
    assert il.snapshot(conn)["instances"][-1]["status"] == "completed"
    assert _locks_of(conn, iid) == []
    assert il.request_route(conn, "R-USE3")["decision"] == "permitted"


# --------------------------------------------------------------------- #
# 待行队列：实体冲突等待、按实体争用越过、解除后兑现
# --------------------------------------------------------------------- #
def test_pending_waits_on_flank_entity_and_grants_after_release(conn):
    il.import_yard(conn, yard_flank())
    il.report_event(conn, ev(1, "D3", "occupy"))  # 侧防所指区段被外部占用
    p = il.submit_pending(conn, "R-FLANK")
    assert p["status"] == "waiting"
    # 等待试探不写只增拒绝
    assert conn.execute("SELECT COUNT(*) c FROM approvals WHERE decision='denied'").fetchone()["c"] == 0
    # 实体占用解除后同事务兑现
    out = il.report_event(conn, ev(2, "D3", "clear"))
    assert [g["pending_id"] for g in out["granted_pending"]] == [p["id"]]
    row = [x for x in il.snapshot(conn)["pending"] if x["id"] == p["id"]][0]
    assert row["status"] == "granted" and row["instance_id"] is not None


def test_pending_overtake_decided_by_entity_contention(conn):
    """受阻前项的侧防实体计入争用：后项直接使用同一实体必须等待，不争用者可越过。"""
    il.import_yard(conn, yard_flank())
    il.report_event(conn, ev(1, "D1", "occupy"))  # 外部占用 S1，使 R-A 受阻
    pa = il.submit_pending(conn, "R-FLANK")  # [S1] + 侧防 S3：S1 占用 -> 等待
    pb = il.submit_pending(conn, "R-USE3")   # 直接用 S3：与前项侧防实体争用 -> 等待
    pc = il.submit_pending(conn, "R-GEN")    # [S4]+generic：不争用 -> 越过放行
    assert pa["status"] == "waiting"
    assert pb["status"] == "waiting", "与受阻前项的侧防实体争用，不得越过"
    assert pc["status"] == "granted"
    assert conn.execute("SELECT COUNT(*) c FROM approvals WHERE decision='denied'").fetchone()["c"] == 0

    # S1 出清：前项兑现并持有 S3 侧防实体锁；与其争用的后项继续等待
    out = il.report_event(conn, ev(2, "D1", "clear"))
    assert [g["pending_id"] for g in out["granted_pending"]] == [pa["id"]]
    assert _status(conn, pb["id"]) == "waiting"

    # 前项跑完（单段进路：占用即推进、出清即完成），S3 侧防锁释放后后项兑现
    il.report_event(conn, ev(3, "D1", "occupy"))
    out = il.report_event(conn, ev(4, "D1", "clear"))
    assert [g["pending_id"] for g in out["granted_pending"]] == [pb["id"]]
    assert _status(conn, pb["id"]) == "granted"


def test_pending_waits_on_direct_user_of_flank_entity_then_grants(conn):
    il.import_yard(conn, yard_flank())
    il.request_route(conn, "R-USE3")  # 直接锁用 S3
    p = il.submit_pending(conn, "R-FLANK")  # 侧防实体 S3 被直接锁用 -> 等待
    assert p["status"] == "waiting"
    # R-USE3 跑完：占用 S3、出清 S3 完成，锁释放后兑现
    il.report_event(conn, ev(1, "D3", "occupy"))
    out = il.report_event(conn, ev(2, "D3", "clear"))
    assert [g["pending_id"] for g in out["granted_pending"]] == [p["id"]]


def _status(conn, pending_id):
    return [x for x in il.snapshot(conn)["pending"] if x["id"] == pending_id][0]["status"]


# --------------------------------------------------------------------- #
# 缺号 / 故障持锁 / 复位
# --------------------------------------------------------------------- #
def test_gap_blocks_pending_even_when_flank_entity_free(conn):
    il.import_yard(conn, yard_flank())
    assert il.report_event(conn, ev(2, "D2", "occupy"))["buffered"] is True  # 缺 #1
    p = il.submit_pending(conn, "R-FLANK")
    assert p["status"] == "waiting"  # 缺号期间全部等待
    out = il.report_event(conn, ev(1, "D4", "occupy"))  # 补齐缺号（S4/S2 外部占用，与 S3 无关）
    assert [g["pending_id"] for g in out["granted_pending"]] == [p["id"]]


def test_fault_holds_flank_locks_then_reset_grants_pending(conn):
    il.import_yard(conn, yard_chain_flank())
    il.request_route(conn, "R-F")
    il.report_event(conn, ev(1, "D1", "occupy"))
    il.report_event(conn, ev(2, "D2", "occupy"))  # 推进到末段 S2，S1 仍占用
    fault = il.report_event(conn, ev(3, "D2", "clear"))  # 末段出清、前段占用 -> 故障
    assert fault["processed"][0]["fault"] is True
    fid = fault["processed"][0]["instance_id"]
    # 故障实例保留侧防实体锁
    assert ("flank_section", "S3") in _locks_of(conn, fid)

    p = il.submit_pending(conn, "R-USE3")  # 争故障实例的侧防实体 -> 等待
    assert p["status"] == "waiting"
    il.report_event(conn, ev(4, "D1", "clear"))  # 外部出清，故障锁仍不释放
    assert _status(conn, p["id"]) == "waiting"
    rr = il.reset_instance(conn, fid)  # 全段空闲，复位成功，同事务兑现
    assert [g["pending_id"] for g in rr["granted_pending"]] == [p["id"]]
    assert _locks_of(conn, fid) == []


def test_reset_blocked_by_foreign_flank_entity_lock(conn):
    """复位前置条件：进路区段被其他进路侧防引用（flank_section 锁）也算他人锁。

    故障实例的 S2 直接锁已在推进中逐段释放，故 R-PROTECT 能获得 S2 的
    侧防实体锁；复位时该侧防实体锁视为“他人锁”阻止复位。
    """
    y = {
        "yard_id": "reset-flank-yard",
        "nodes": [{"id": n} for n in ("A", "B", "C", "D", "E", "F")],
        "sections": [
            {"id": "S1", "a": "A", "b": "B", "detector": "D1"},
            {"id": "S2", "a": "B", "b": "C", "detector": "D2"},
            {"id": "S3", "a": "C", "b": "D", "detector": "D3"},
            {"id": "S4", "a": "E", "b": "F", "detector": "D4"},
        ],
        "switches": [],
        "resources": [{"id": "FP-S2", "kind": "section", "ref": "S2"}],
        "routes": [
            {"id": "R-F", "sections": ["S1", "S2", "S3"], "switches": {}, "flank": []},
            {"id": "R-PROTECT", "sections": ["S4"], "switches": {}, "flank": ["FP-S2"]},
        ],
    }
    il.import_yard(conn, y)
    il.request_route(conn, "R-F")
    il.report_event(conn, ev(1, "D1", "occupy"))  # S1 占用（保持占用）
    il.report_event(conn, ev(2, "D2", "occupy"))  # 推进 S2；S1 仍占用 -> S1 锁保留
    il.report_event(conn, ev(3, "D2", "clear"))   # S2 出清，后继未占用 -> 等待
    il.report_event(conn, ev(4, "D3", "occupy"))  # 推进末段；S2 已出清 -> 释放 S2 直接锁
    # S2 直接锁已释放：R-PROTECT 获得 S2 的侧防实体锁
    assert il.request_route(conn, "R-PROTECT")["decision"] == "permitted"
    # 末段出清，S1 仍由本实例锁定且占用 -> 故障
    fault = il.report_event(conn, ev(5, "D3", "clear"))
    assert fault["processed"][0]["fault"] is True
    fid = fault["processed"][0]["instance_id"]
    # S1 外部出清后占用条件已满足，但 S2 被他人侧防引用 -> 复位受阻
    il.report_event(conn, ev(6, "D1", "clear"))
    with pytest.raises(il.ClientError) as ei:
        il.reset_instance(conn, fid)
    assert ei.value.code == "reset_blocked"
    assert any(x["type"] == "foreign_lock" for x in ei.value.extra["reasons"])
    # 侧防进路跑完释放实体锁后，复位成功
    il.report_event(conn, ev(7, "D4", "occupy"))
    il.report_event(conn, ev(8, "D4", "clear"))
    assert il.reset_instance(conn, fid)["reset"] is True


# --------------------------------------------------------------------- #
# 重启后已持有的侧防锁继续有效
# --------------------------------------------------------------------- #
def test_restart_keeps_flank_locks_effective(conn):
    il.import_yard(conn, yard_flank())
    il.request_route(conn, "R-FLANK")  # 持 flank_section S3
    p = il.submit_pending(conn, "R-USE3")
    assert p["status"] == "waiting"
    dbfile = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()

    c2 = connect(dbfile)
    try:
        snap = il.snapshot(c2)
        assert any(l["resource_type"] == "flank_section" and l["resource_id"] == "S3" for l in snap["locks"])
        # 启动恢复：侧防锁仍有效，不兑现冲突待行项
        assert il.startup_recover(c2)["granted"] == []
        # 重启后即时申请仍被侧防实体锁阻止
        r = il.request_route(c2, "R-USE3")
        assert r["decision"] == "denied"
        assert any(c.get("held_as") == "flank_section" for c in r["conflicts"])
        # 原进路跑完后释放，待行兑现
        il.report_event(c2, ev(1, "D1", "occupy"))
        out = il.report_event(c2, ev(2, "D1", "clear"))
        assert [g["pending_id"] for g in out["granted_pending"]] == [p["id"]]
    finally:
        c2.close()


# --------------------------------------------------------------------- #
# REST 端到端：红灯响应携带实体冲突依据
# --------------------------------------------------------------------- #
@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import importlib
    import app.db as dbmod
    import app.interlocking as ilmod
    import app.main as mainmod

    monkeypatch.setenv("INTERLOCKING_DB", str(tmp_path / "flank_api.db"))
    importlib.reload(dbmod)
    importlib.reload(ilmod)
    importlib.reload(mainmod)
    mainmod.init_db = dbmod.init_db
    with TestClient(mainmod.app) as c:
        yield c


def test_api_flank_entity_conflict_basis(client):
    assert client.post("/api/yard", json=yard_flank()).status_code == 200
    client.post("/api/events", json={"seq": 1, "detector_id": "D3", "kind": "occupy"})
    r = client.post("/api/routes/request", json={"route_id": "R-FLANK"})
    body = r.json()
    assert body["signal"] == "red"
    assert any(c["type"] == "section_occupied" and c.get("via_flank") == "FP-S3" for c in body["conflicts"])
    # 状态快照中待行/锁结构正常
    assert client.get("/api/state").json()["occupancy"]["S3"] is True
