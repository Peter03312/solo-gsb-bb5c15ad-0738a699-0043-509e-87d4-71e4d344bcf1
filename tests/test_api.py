"""HTTP API 集成测试（临时 SQLite + TestClient）。"""

from __future__ import annotations

import os
import tempfile
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("QSO_DB_PATH", tmp.name)
    # config 在调用时读环境变量；确保 app 启动初始化用的是临时库。
    from app import database
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


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_full_flow_pairing(client):
    r = client.post(
        "/drills",
        json={"drill_id": "EX-2026", "allowed_time_diff_seconds": 10},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 0 and body["sealed"] is False

    payload = {"logs": [
        _log("r1", "BH1AAA", "BH2BBB", "2026-10-04T08:00:00+08:00"),
        _log("r2", "BH2BBB", "BH1AAA", "2026-10-04T00:00:05+00:00"),
        _log("r3", "BH3CCC", "BH4DDD", "2026-10-04T00:00:00+00:00"),
    ]}
    r = client.post("/drills/EX-2026/logs", json=payload)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["version"] == 1
    assert len(result["pairs"]) == 1
    assert result["pairs"][0]["record_a"] == "r1"
    assert result["pairs"][0]["record_b"] == "r2"
    assert result["pairs"][0]["time_diff_seconds"] == 5
    assert result["unpaired"] == ["r3"]
    assert result["total_time_diff_seconds"] == 5


def test_negative_diff_rejected(client):
    r = client.post(
        "/drills",
        json={"drill_id": "X", "allowed_time_diff_seconds": -1},
    )
    assert r.status_code == 422
    assert "非负" in r.text


def test_naive_time_rejected(client):
    client.post("/drills", json={"drill_id": "X", "allowed_time_diff_seconds": 1})
    r = client.put(
        "/drills/X/logs/r1",
        json=_log("r1", "A", "B", "2026-10-04T08:00:00"),
    )
    assert r.status_code == 422
    assert "时区" in r.text


def test_same_station_counterpart_rejected(client):
    client.post("/drills", json={"drill_id": "X", "allowed_time_diff_seconds": 1})
    r = client.put(
        "/drills/X/logs/r1",
        json=_log("r1", "BH1AAA", "BH1AAA", "2026-10-04T08:00:00+08:00"),
    )
    assert r.status_code == 422
    assert "不能相同" in r.text


def test_unknown_drill_errors(client):
    r = client.get("/drills/NOPE/result")
    assert r.status_code == 404
    assert r.json()["error"] == "drill_not_found"

    r = client.put(
        "/drills/NOPE/logs/1",
        json=_log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
    )
    assert r.status_code == 404


def test_duplicate_drill_conflict(client):
    client.post("/drills", json={"drill_id": "X", "allowed_time_diff_seconds": 1})
    r = client.post("/drills", json={"drill_id": "X", "allowed_time_diff_seconds": 2})
    assert r.status_code == 409
    assert r.json()["error"] == "drill_exists"


def test_retransmit_no_version_bump_and_correction_recomputes(client):
    client.post("/drills", json={"drill_id": "D", "allowed_time_diff_seconds": 10})
    entry = _log("1", "A", "B", "2026-10-04T00:00:00+00:00")
    r1 = client.put("/drills/D/logs/1", json=entry).json()
    assert r1["version"] == 1
    # 同内容重传（大小写/空白差异视为同一内容）
    same = dict(entry, station=" a ", counterpart="b", band="40M")
    r2 = client.put("/drills/D/logs/1", json=same).json()
    assert r2["version"] == 1
    # 异内容更正 -> 版本推进并重算
    fixed = dict(entry, time="2026-10-04T00:00:09+00:00")
    client.put(
        "/drills/D/logs/2",
        json=_log("2", "B", "A", "2026-10-04T00:00:00+00:00"),
    )
    r3 = client.put("/drills/D/logs/1", json=fixed).json()
    assert r3["version"] == 3
    assert len(r3["pairs"]) == 1
    assert r3["pairs"][0]["time_diff_seconds"] == 9


def test_correction_can_break_pairing(client):
    client.post("/drills", json={"drill_id": "D", "allowed_time_diff_seconds": 5})
    client.put("/drills/D/logs/1",
               json=_log("1", "A", "B", "2026-10-04T00:00:00+00:00"))
    r = client.put("/drills/D/logs/2",
                   json=_log("2", "B", "A", "2026-10-04T00:00:03+00:00")).json()
    assert len(r["pairs"]) == 1
    # 把记录 2 更正到 100 秒后 -> 不再互证
    r = client.put("/drills/D/logs/2",
                   json=_log("2", "B", "A", "2026-10-04T00:01:40+00:00")).json()
    assert r["pairs"] == []
    assert set(r["unpaired"]) == {"1", "2"}


def test_seal_blocks_writes_but_allow_queries(client):
    client.post("/drills", json={"drill_id": "D", "allowed_time_diff_seconds": 5})
    client.put("/drills/D/logs/1",
               json=_log("1", "A", "B", "2026-10-04T00:00:00+00:00"))
    client.put("/drills/D/logs/2",
               json=_log("2", "B", "A", "2026-10-04T00:00:01+00:00"))

    r = client.post("/drills/D/seal")
    assert r.status_code == 200 and r.json()["sealed"] is True

    r = client.put("/drills/D/logs/3",
                   json=_log("3", "A", "B", "2026-10-04T00:00:00+00:00"))
    assert r.status_code == 409 and r.json()["error"] == "drill_sealed"

    r = client.post("/drills/D/logs", json={"logs": [
        _log("9", "A", "B", "2026-10-04T00:00:00+00:00")]})
    assert r.status_code == 409

    # 仍可查询
    r = client.get("/drills/D/result")
    assert r.status_code == 200 and r.json()["sealed"] is True
    assert len(r.json()["pairs"]) == 1
    assert client.get("/drills/D/logs").status_code == 200

    # 重复封存同样 409
    assert client.post("/drills/D/seal").status_code == 409


def test_batch_duplicate_record_no_rejected(client):
    client.post("/drills", json={"drill_id": "D", "allowed_time_diff_seconds": 5})
    r = client.post("/drills/D/logs", json={"logs": [
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("1", "A", "B", "2026-10-04T00:00:01+00:00"),
    ]})
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_record_in_batch"
    # 整批回滚：无日志写入
    assert client.get("/drills/D/logs").json() == []


def test_batch_invalid_entire_batch_rollback(client):
    client.post("/drills", json={"drill_id": "D", "allowed_time_diff_seconds": 5})
    r = client.post("/drills/D/logs", json={"logs": [
        _log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("2", "A", "A", "2026-10-04T00:00:01+00:00"),
    ]})
    assert r.status_code == 422
    assert client.get("/drills/D/logs").json() == []


def test_concurrent_writes_are_serialized(client):
    client.post("/drills", json={"drill_id": "D", "allowed_time_diff_seconds": 600})
    errors = []

    def put(no, t):
        try:
            resp = client.put(
                f"/drills/D/logs/{no}",
                json=_log(no, "A", "B", f"2026-10-04T00:00:{t:02d}+00:00"),
            )
            assert resp.status_code == 200, resp.text
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=put, args=(f"n{i}", i % 60))
        for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    result = client.get("/drills/D/result").json()
    # 每次异内容写入都推进版本，最终版本应为 20。
    assert result["version"] == 20
    assert len(client.get("/drills/D/logs").json()) == 20
