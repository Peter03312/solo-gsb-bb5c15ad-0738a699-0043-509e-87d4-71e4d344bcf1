"""复核员锁定功能的 HTTP API 集成测试。"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("QSO_DB_PATH", tmp.name)
    from app import database  # noqa: F401
    from app.main import app

    with TestClient(app) as c:
        yield c
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except OSError:
            pass


def _log(no, station, counterpart, t, band="40m", mode="SSB"):
    return {
        "record_no": no,
        "station": station,
        "counterpart": counterpart,
        "time": t,
        "band": band,
        "mode": mode,
    }


def _make_drill(client, drill="D", allowed=50):
    r = client.post(
        "/drills",
        json={"drill_id": drill, "allowed_time_diff_seconds": allowed},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _put_logs(client, drill="D", logs=()):
    if len(logs) == 1:
        r = client.put(f"/drills/{drill}/logs/{logs[0]['record_no']}",
                       json=logs[0])
    else:
        r = client.post(f"/drills/{drill}/logs", json={"logs": list(logs)})
    assert r.status_code == 200, r.text
    return r.json()


def _lock(client, a, b, drill="D"):
    return client.put(f"/drills/{drill}/locks/{a}/{b}")


def _unlock(client, a, b, drill="D"):
    return client.delete(f"/drills/{drill}/locks/{a}/{b}")


# --------------------------------------------------------------------------
# 锁定成功路径
# --------------------------------------------------------------------------
def test_lock_pair_forced_into_result(client):
    _make_drill(client)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "B", "A", "2026-10-04T00:00:05+00:00"),
    ])
    r = _lock(client, "1", "2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 2  # 写入 1 次 + 锁定 1 次
    assert body["pairs"] == [
        {"record_a": "1", "record_b": "2", "time_diff_seconds": 5.0}
    ]
    assert body["unpaired"] == []
    assert body["total_time_diff_seconds"] == 5.0

    locks = client.get("/drills/D/locks").json()
    assert locks["version"] == 2
    assert len(locks["locks"]) == 1
    item = locks["locks"][0]
    assert item["record_a"] == "1" and item["record_b"] == "2"
    assert item["time_diff_seconds"] == 5.0
    assert "locked_at" in item and item["locked_at"]


def test_lock_order_independent_and_pair_internal_sorted(client):
    _make_drill(client)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "B", "A", "2026-10-04T00:00:03+00:00"),
    ])
    # 反序提交，内部仍升序落库
    r = _lock(client, "2", "1")
    assert r.status_code == 200, r.text
    assert r.json()["pairs"][0]["record_a"] == "1"
    assert r.json()["pairs"][0]["record_b"] == "2"
    item = client.get("/drills/D/locks").json()["locks"][0]
    assert (item["record_a"], item["record_b"]) == ("1", "2")


def test_duplicate_lock_is_retransmit_no_version_bump(client):
    _make_drill(client)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "B", "A", "2026-10-04T00:00:05+00:00"),
    ])
    r1 = _lock(client, "1", "2").json()
    assert r1["version"] == 2
    # 同序重传与反序重传均视为重传
    r2 = _lock(client, "1", "2").json()
    r3 = _lock(client, "2", "1").json()
    assert r2["version"] == 2 and r3["version"] == 2
    assert len(client.get("/drills/D/locks").json()["locks"]) == 1


def test_unlock_restores_free_matching_and_bumps_version(client):
    _make_drill(client)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "B", "A", "2026-10-04T00:00:05+00:00"),
    ])
    _lock(client, "1", "2")
    r = _unlock(client, "1", "2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 3  # 写入 1 + 锁定 1 + 解除 1
    assert [p["record_a"] for p in body["pairs"]] == ["1"]
    assert client.get("/drills/D/locks").json()["locks"] == []
    # 反序解除同样命中
    _lock(client, "1", "2")
    r = _unlock(client, "2", "1")
    assert r.status_code == 200
    assert client.get("/drills/D/locks").json()["locks"] == []


def test_unlock_missing_lock_returns_404(client):
    _make_drill(client)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "B", "A", "2026-10-04T00:00:05+00:00"),
    ])
    r = _unlock(client, "1", "2")
    assert r.status_code == 404
    assert r.json()["error"] == "lock_not_found"
    # 解除失败不推进版本
    assert client.get("/drills/D/result").json()["version"] == 1


# --------------------------------------------------------------------------
# 锁定校验：现存 / 互指 / 波段 / 模式 / 时差 / 占用
# --------------------------------------------------------------------------
def test_lock_requires_existing_records(client):
    _make_drill(client)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
    ])
    r = _lock(client, "1", "9")
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_lock_candidate"
    assert "不存在" in r.json()["message"]
    assert client.get("/drills/D/result").json()["version"] == 1
    assert client.get("/drills/D/locks").json()["locks"] == []


def test_lock_requires_mutual_reference(client):
    _make_drill(client)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "A", "B", "2026-10-04T00:00:01+00:00"),
    ])
    r = _lock(client, "1", "2")
    assert r.status_code == 422
    assert "相互指认" in r.json()["message"]


def test_lock_requires_same_band(client):
    _make_drill(client)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00", band="40m"),
        _log("2", "B", "A", "2026-10-04T00:00:01+00:00", band="20m"),
    ])
    r = _lock(client, "1", "2")
    assert r.status_code == 422 and "波段" in r.json()["message"]


def test_lock_requires_same_mode(client):
    _make_drill(client)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00", mode="SSB"),
        _log("2", "B", "A", "2026-10-04T00:00:01+00:00", mode="CW"),
    ])
    r = _lock(client, "1", "2")
    assert r.status_code == 422 and "模式" in r.json()["message"]


def test_lock_requires_time_diff_within_limit(client):
    _make_drill(client, allowed=5)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "B", "A", "2026-10-04T00:00:08+00:00"),
    ])
    r = _lock(client, "1", "2")
    assert r.status_code == 422 and "超过允许值" in r.json()["message"]


def test_lock_same_record_no_rejected(client):
    _make_drill(client)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
    ])
    r = _lock(client, "1", "1")
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_lock_candidate"


def test_locked_record_cannot_be_occupied_again(client):
    _make_drill(client)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "B", "A", "2026-10-04T00:00:01+00:00"),
        _log("3", "B", "A", "2026-10-04T00:00:02+00:00"),
    ])
    assert _lock(client, "1", "2").status_code == 200
    # 1 已在锁定 (1,2) 中：(1,3) 合法但记录 1 被占用
    r = _lock(client, "1", "3")
    assert r.status_code == 409
    assert r.json()["error"] == "lock_record_busy"
    # 重复锁 (1,2) 仍视为重传成功
    assert _lock(client, "1", "2").status_code == 200


# --------------------------------------------------------------------------
# 锁定与自由匹配的组合语义
# --------------------------------------------------------------------------
def test_locked_pair_removed_from_free_matching(client):
    _make_drill(client, allowed=50)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),  # 左
        _log("2", "B", "A", "2026-10-04T00:00:00+00:00"),  # 右
        _log("3", "B", "A", "2026-10-04T00:00:00+00:00"),  # 右
        _log("4", "A", "B", "2026-10-04T00:00:00+00:00"),  # 左
    ])
    # 自由最优：(1,2)（字典序最小），(3,4) 次之。锁定 (1,3) 后：
    # 1/3 移出自由匹配，剩余左 4 / 右 2 自由配成一对 (2,4)。
    r = _lock(client, "1", "3")
    assert r.status_code == 200, r.text
    assert [(p["record_a"], p["record_b"]) for p in r.json()["pairs"]] == [
        ("1", "3"), ("2", "4"),
    ]
    assert r.json()["unpaired"] == []


def test_lock_not_replaced_even_if_pair_count_drops(client):
    # 左: 1(t0), 4(t100)；右: 2(t0), 3(t50)；允许时差 50
    # 合法边: (1,2)=0, (1,3)=50, (4,3)=50 —— 自由最优为两对
    # (1,2)+(3,4)，总时差 50。锁定 (1,3)=50 后 4 与 2 互指但时差 100
    # 超限无法配对 => 全场只剩 1 对，锁定仍不得被替换。
    _make_drill(client, allowed=50)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "B", "A", "2026-10-04T00:00:00+00:00"),
        _log("3", "B", "A", "2026-10-04T00:00:50+00:00"),
        _log("4", "A", "B", "2026-10-04T00:01:40+00:00"),
    ])
    free = client.get("/drills/D/result").json()
    assert [(p["record_a"], p["record_b"]) for p in free["pairs"]] == [
        ("1", "2"), ("3", "4"),
    ]
    r = _lock(client, "1", "3")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(p["record_a"], p["record_b"]) for p in body["pairs"]] == [
        ("1", "3"),
    ]
    assert set(body["unpaired"]) == {"2", "4"}
    assert body["total_time_diff_seconds"] == 50.0

    # 解除后自由最优恢复
    r = _unlock(client, "1", "3").json()
    assert [(p["record_a"], p["record_b"]) for p in r["pairs"]] == [
        ("1", "2"), ("3", "4"),
    ]
    assert r["total_time_diff_seconds"] == 50.0


def test_free_matching_still_lexicographically_minimal_with_lock(client):
    # 锁定 (1,4)（差 10），剩余 2/3 同差 0 => 唯一一对 (2,3)
    _make_drill(client, allowed=10)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("3", "B", "A", "2026-10-04T00:00:00+00:00"),
        _log("4", "B", "A", "2026-10-04T00:00:10+00:00"),
    ])
    r = _lock(client, "1", "4").json()
    assert [(p["record_a"], p["record_b"]) for p in r["pairs"]] == [
        ("1", "4"), ("2", "3"),
    ]
    assert r["total_time_diff_seconds"] == 10.0


# --------------------------------------------------------------------------
# 更正使锁定失效 => 拒绝写入、状态不变（单条与批量）
# --------------------------------------------------------------------------
def test_single_correction_breaking_lock_is_rejected(client):
    _make_drill(client, allowed=5)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "B", "A", "2026-10-04T00:00:03+00:00"),
    ])
    _lock(client, "1", "2")
    version_before = client.get("/drills/D/result").json()["version"]

    # 把记录 2 改到 100 秒后 -> 锁定 (1,2) 时差超限 => 拒绝
    r = client.put(
        "/drills/D/logs/2",
        json=_log("2", "B", "A", "2026-10-04T00:01:40+00:00"),
    )
    assert r.status_code == 409
    assert r.json()["error"] == "lock_invalidated_by_write"

    # 日志内容、版本、结果、锁定全部保持不变
    logs = client.get("/drills/D/logs").json()
    assert logs[1]["time"].startswith("2026-10-04T00:00:03")
    result = client.get("/drills/D/result").json()
    assert result["version"] == version_before
    assert [(p["record_a"], p["record_b"]) for p in result["pairs"]] == [
        ("1", "2")
    ]
    assert len(client.get("/drills/D/locks").json()["locks"]) == 1


def test_single_correction_still_valid_for_lock_is_accepted(client):
    _make_drill(client, allowed=10)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "B", "A", "2026-10-04T00:00:03+00:00"),
    ])
    _lock(client, "1", "2")
    # 时差 3 -> 8，仍在允许范围内：接受更正，锁定保留，重算结果。
    r = client.put(
        "/drills/D/logs/2",
        json=_log("2", "B", "A", "2026-10-04T00:00:08+00:00"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pairs"][0]["time_diff_seconds"] == 8.0
    locks = client.get("/drills/D/locks").json()
    assert locks["locks"][0]["time_diff_seconds"] == 8.0


def test_batch_correction_breaking_lock_rejects_entire_batch(client):
    _make_drill(client, allowed=5)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "B", "A", "2026-10-04T00:00:03+00:00"),
        _log("3", "C", "D", "2026-10-04T00:00:00+00:00"),
    ])
    _lock(client, "1", "2")
    version_before = client.get("/drills/D/result").json()["version"]

    # 同批：3 是无关新增（本应合法），但 2 的更正使锁定失效 => 整批回滚
    r = client.post("/drills/D/logs", json={"logs": [
        _log("2", "B", "A", "2026-10-04T00:01:40+00:00"),
        _log("3", "C", "D", "2026-10-04T00:00:00+00:00"),
        _log("4", "D", "C", "2026-10-04T00:00:01+00:00"),
    ]})
    assert r.status_code == 409
    assert r.json()["error"] == "lock_invalidated_by_write"

    logs = client.get("/drills/D/logs").json()
    assert {x["record_no"] for x in logs} == {"1", "2", "3"}
    assert logs[1]["time"].startswith("2026-10-04T00:00:03")
    result = client.get("/drills/D/result").json()
    assert result["version"] == version_before


def test_retransmit_under_lock_no_version_bump(client):
    _make_drill(client)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "B", "A", "2026-10-04T00:00:03+00:00"),
    ])
    _lock(client, "1", "2")
    before = client.get("/drills/D/result").json()["version"]
    r = client.put(
        "/drills/D/logs/1",
        json=_log("1", " a ", " b ", "2026-10-04T00:00:00+00:00"),
    )
    assert r.status_code == 200
    assert r.json()["version"] == before


# --------------------------------------------------------------------------
# 封存与未知演练
# --------------------------------------------------------------------------
def test_sealed_drill_rejects_lock_operations(client):
    _make_drill(client)
    _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "B", "A", "2026-10-04T00:00:03+00:00"),
    ])
    _lock(client, "1", "2")
    client.post("/drills/D/seal")

    r = _lock(client, "1", "2")
    assert r.status_code == 409 and r.json()["error"] == "drill_sealed"

    r = _unlock(client, "1", "2")
    assert r.status_code == 409 and r.json()["error"] == "drill_sealed"

    # 锁定清单查询仍可用
    r = client.get("/drills/D/locks")
    assert r.status_code == 200
    assert len(r.json()["locks"]) == 1
    # 结果查询不受影响
    assert client.get("/drills/D/result").status_code == 200


def test_lock_ops_on_unknown_drill_404(client):
    assert _lock(client, "1", "2", drill="NOPE").status_code == 404
    assert _unlock(client, "1", "2", drill="NOPE").status_code == 404
    r = client.get("/drills/NOPE/locks")
    assert r.status_code == 404
    assert r.json()["error"] == "drill_not_found"


def test_lock_list_empty_drill(client):
    _make_drill(client)
    body = client.get("/drills/D/locks").json()
    assert body == {
        "drill_id": "D", "version": 0, "sealed": False, "locks": [],
    }


def test_results_query_shape_unchanged_without_locks(client):
    # 未做任何锁定时，结果与写入接口的响应保持历史兼容结构。
    _make_drill(client)
    r = _put_logs(client, logs=[
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "B", "A", "2026-10-04T00:00:05+00:00"),
        _log("3", "C", "D", "2026-10-04T00:00:00+00:00"),
    ])
    assert set(r.keys()) == {
        "drill_id", "version", "sealed", "pairs", "unpaired",
        "total_time_diff_seconds", "recomputed_at",
    }
