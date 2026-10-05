"""SQLite 持久化层。

并发写入由 SQLite 的写事务串行化保证：每次写入都开启
``BEGIN IMMEDIATE`` 立即获取 RESERVED 锁，第二个并发写入会在获取锁时
阻塞/报忙；配合 busy_timeout 重试，所有写入被串行提交。版本号、
日志与配对结果在同一个事务中更新，保证原子性。

启用 WAL 模式以支持“封存/计算期间仍可并发查询最终结果”。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable

from . import config
from .matching import MatchResult, Pair, Record, match_records

MICROS_PER_SECOND = 1_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS drills (
    drill_id           TEXT PRIMARY KEY,
    allowed_diff_secs  REAL NOT NULL CHECK (allowed_diff_secs >= 0),
    version            INTEGER NOT NULL DEFAULT 0,
    sealed             INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    sealed_at          TEXT
);

CREATE TABLE IF NOT EXISTS logs (
    drill_id     TEXT NOT NULL,
    record_no    TEXT NOT NULL,
    station      TEXT NOT NULL,
    counterpart  TEXT NOT NULL,
    ts_micros    INTEGER NOT NULL,
    ts_iso       TEXT NOT NULL,
    band         TEXT NOT NULL,
    mode         TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (drill_id, record_no),
    FOREIGN KEY (drill_id) REFERENCES drills(drill_id)
);

CREATE TABLE IF NOT EXISTS results (
    drill_id                  TEXT PRIMARY KEY,
    version                   INTEGER NOT NULL,
    pairs_json                TEXT NOT NULL,
    unpaired_json             TEXT NOT NULL,
    total_time_diff_seconds   REAL NOT NULL,
    recomputed_at             TEXT NOT NULL,
    FOREIGN KEY (drill_id) REFERENCES drills(drill_id)
);

-- 复核员手动锁定的互证候选对。record_a <= record_b（字典序），
-- 与 results 的 pairs 内部排序规则一致；(drill_id, record_a,
-- record_b) 唯一，且同一演练内任一记录号最多出现在一条锁定中
-- （由服务层在同一写事务内校验）。
CREATE TABLE IF NOT EXISTS locks (
    drill_id     TEXT NOT NULL,
    record_a     TEXT NOT NULL,
    record_b     TEXT NOT NULL,
    locked_at    TEXT NOT NULL,
    PRIMARY KEY (drill_id, record_a, record_b),
    FOREIGN KEY (drill_id) REFERENCES drills(drill_id)
);
CREATE INDEX IF NOT EXISTS idx_locks_drill ON locks (drill_id);
"""


# --------------------------------------------------------------------------
# 领域错误
# --------------------------------------------------------------------------
class ServiceError(Exception):
    """业务错误基类，附带 HTTP 状态码与机器可读错误码。"""

    status_code = 400
    error_code = "bad_request"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class DrillNotFoundError(ServiceError):
    status_code = 404
    error_code = "drill_not_found"


class DrillExistsError(ServiceError):
    status_code = 409
    error_code = "drill_exists"


class SealedError(ServiceError):
    status_code = 409
    error_code = "drill_sealed"


class DuplicateRecordInBatchError(ServiceError):
    status_code = 409
    error_code = "duplicate_record_in_batch"


class InvalidLockCandidateError(ServiceError):
    """锁定候选不合法：记录不存在/不互指/波段模式不同/时差超限/同号。"""

    status_code = 422
    error_code = "invalid_lock_candidate"


class LockRecordBusyError(ServiceError):
    """候选记录之一已被其他锁定占用。"""

    status_code = 409
    error_code = "lock_record_busy"


class LockNotFoundError(ServiceError):
    """解除一条不存在的锁定。"""

    status_code = 404
    error_code = "lock_not_found"


class LockInvalidatedByWriteError(ServiceError):
    """单条/批量更正使现存锁定失效，整次写入拒绝。"""

    status_code = 409
    error_code = "lock_invalidated_by_write"


# --------------------------------------------------------------------------
# 连接管理
# --------------------------------------------------------------------------
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.database_url(),
        timeout=30.0,
        isolation_level=None,  # 手工控制事务（autocommit 模式）
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """创建数据目录、表结构并开启 WAL（幂等）。"""
    import os

    db_path = config.database_url()
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def _write_tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 写事务：立即拿写锁，提交/回滚原子。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------
# 序列化辅助
# --------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _micros_to_iso(ts_micros: int) -> str:
    return datetime.fromtimestamp(
        ts_micros / MICROS_PER_SECOND, tz=timezone.utc
    ).isoformat()


def _record_from_row(row: sqlite3.Row) -> Record:
    return Record(
        record_no=row["record_no"],
        station=row["station"],
        counterpart=row["counterpart"],
        ts_micros=row["ts_micros"],
        band=row["band"],
        mode=row["mode"],
    )


def _load_records(conn: sqlite3.Connection, drill_id: str) -> list[Record]:
    rows = conn.execute(
        "SELECT * FROM logs WHERE drill_id = ? ORDER BY rowid", (drill_id,)
    ).fetchall()
    return [_record_from_row(r) for r in rows]


def _load_lock_keys(
    conn: sqlite3.Connection, drill_id: str
) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT record_a, record_b FROM locks WHERE drill_id = ? "
        "ORDER BY record_a, record_b",
        (drill_id,),
    ).fetchall()
    return [(r["record_a"], r["record_b"]) for r in rows]


def _check_lock_candidate(
    by_no: dict[str, sqlite3.Row],
    a_no: str,
    b_no: str,
    allowed_diff_secs: float,
) -> None:
    """校验一对候选是否满足“现存、互指、同波段同模式、时差不超限”。

    入参为记录号 -> logs 行的映射；任一条件不满足即抛
    InvalidLockCandidateError（中文消息指明原因）。
    """
    if a_no == b_no:
        raise InvalidLockCandidateError(
            f"不能锁定同一条记录: {a_no}"
        )
    ra = by_no.get(a_no)
    rb = by_no.get(b_no)
    missing = [no for no, row in ((a_no, ra), (b_no, rb)) if row is None]
    if missing:
        raise InvalidLockCandidateError(
            "锁定失败，记录号不存在: " + ", ".join(sorted(missing))
        )
    if not (
        ra["station"] == rb["counterpart"]
        and ra["counterpart"] == rb["station"]
    ):
        raise InvalidLockCandidateError(
            f"记录 {a_no} 与 {b_no} 未相互指认，不能锁定"
        )
    if ra["band"] != rb["band"]:
        raise InvalidLockCandidateError(
            f"记录 {a_no} 与 {b_no} 波段不同（{ra['band']} / "
            f"{rb['band']}），不能锁定"
        )
    if ra["mode"] != rb["mode"]:
        raise InvalidLockCandidateError(
            f"记录 {a_no} 与 {b_no} 模式不同（{ra['mode']} / "
            f"{rb['mode']}），不能锁定"
        )
    allowed_micros = int(round(allowed_diff_secs * MICROS_PER_SECOND))
    if abs(ra["ts_micros"] - rb["ts_micros"]) > allowed_micros:
        raise InvalidLockCandidateError(
            f"记录 {a_no} 与 {b_no} 时差 "
            f"{abs(ra['ts_micros'] - rb['ts_micros']) / MICROS_PER_SECOND:g}"
            f" 秒超过允许值 {allowed_diff_secs:g} 秒，不能锁定"
        )


def _validate_existing_locks(
    conn: sqlite3.Connection,
    drill: sqlite3.Row,
    records: list[Record] | None = None,
) -> None:
    """更正写入后调用：任一现存锁定失效即抛错（调用方整事务回滚）。

    锁定行的存在性由外键+服务层保证，这里只需重新校验配对合法性；
    锁定对之间不可能共用记录（入库时已强制）。
    """
    if records is None:
        records = _load_records(conn, drill["drill_id"])
    by_no = {r.record_no: r for r in records}
    allowed = drill["allowed_diff_secs"]
    allowed_micros = int(round(allowed * MICROS_PER_SECOND))
    for a_no, b_no in _load_lock_keys(conn, drill["drill_id"]):
        ra = by_no.get(a_no)
        rb = by_no.get(b_no)
        if (
            ra is None
            or rb is None
            or not (
                ra.station == rb.counterpart
                and ra.counterpart == rb.station
            )
            or ra.band != rb.band
            or ra.mode != rb.mode
            or abs(ra.ts_micros - rb.ts_micros) > allowed_micros
        ):
            raise LockInvalidatedByWriteError(
                f"本次写入会使锁定对 ({a_no}, {b_no}) 失效，"
                "整次写入已拒绝，状态不变；请先解除相关锁定"
            )


def _persist_result(
    conn: sqlite3.Connection, drill_id: str, version: int, result: MatchResult
) -> None:
    pairs_payload = [
        {
            "record_a": p.record_a,
            "record_b": p.record_b,
            "time_diff_seconds": p.time_diff_seconds,
        }
        for p in result.pairs
    ]
    conn.execute(
        """
        INSERT INTO results (drill_id, version, pairs_json, unpaired_json,
                             total_time_diff_seconds, recomputed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(drill_id) DO UPDATE SET
            version = excluded.version,
            pairs_json = excluded.pairs_json,
            unpaired_json = excluded.unpaired_json,
            total_time_diff_seconds = excluded.total_time_diff_seconds,
            recomputed_at = excluded.recomputed_at
        """,
        (
            drill_id,
            version,
            json.dumps(pairs_payload, ensure_ascii=False),
            json.dumps(list(result.unpaired), ensure_ascii=False),
            result.total_time_diff_seconds,
            _now_iso(),
        ),
    )


def _recompute(conn: sqlite3.Connection, drill_id: str) -> None:
    """重算全场配对：版本 +1（调用方负责），结果与版本同事务落库。

    先保留全部锁定对（其记录不参与自由匹配），再对剩余日志沿用
    “配对数最多、总时差最小、记录号字典序最小”规则求解；即使锁定
    使全场配对数下降也不会替换锁定。
    """
    drill = _get_drill_row(conn, drill_id)
    version = drill["version"] + 1
    records = _load_records(conn, drill_id)
    locked_pairs = _load_lock_keys(conn, drill_id)
    result = match_records(
        records, drill["allowed_diff_secs"], locked_pairs
    )
    conn.execute(
        "UPDATE drills SET version = ? WHERE drill_id = ?",
        (version, drill_id),
    )
    _persist_result(conn, drill_id, version, result)


def _get_drill_row(
    conn: sqlite3.Connection, drill_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM drills WHERE drill_id = ?", (drill_id,)
    ).fetchone()
    if row is None:
        raise DrillNotFoundError(f"演练号不存在: {drill_id}")
    return row


def _require_unsealed(row: sqlite3.Row) -> None:
    if row["sealed"]:
        raise SealedError(f"演练 {row['drill_id']} 已封存，拒绝写入")


# --------------------------------------------------------------------------
# 对外服务操作
# --------------------------------------------------------------------------
def create_drill(drill_id: str, allowed_diff_secs: float) -> dict:
    conn = connect()
    try:
        with _write_tx(conn):
            exists = conn.execute(
                "SELECT 1 FROM drills WHERE drill_id = ?", (drill_id,)
            ).fetchone()
            if exists is not None:
                raise DrillExistsError(f"演练号已存在: {drill_id}")
            conn.execute(
                "INSERT INTO drills (drill_id, allowed_diff_secs, version, "
                "sealed, created_at) VALUES (?, ?, 0, 0, ?)",
                (drill_id, allowed_diff_secs, _now_iso()),
            )
            # 空场结果：版本 0，无配对。
            _persist_result(conn, drill_id, 0, MatchResult((), (), 0.0))
        return get_drill(drill_id)
    finally:
        conn.close()


def upsert_log(
    drill_id: str,
    record_no: str,
    station: str,
    counterpart: str,
    ts_micros: int,
    band: str,
    mode: str,
) -> dict:
    """写入/更正单条日志；同内容重传不推进版本，异内容更正后重算全场。"""
    conn = connect()
    try:
        changed = False
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            row = conn.execute(
                "SELECT * FROM logs WHERE drill_id = ? AND record_no = ?",
                (drill_id, record_no),
            ).fetchone()
            ts_iso = _micros_to_iso(ts_micros)
            if row is None:
                conn.execute(
                    "INSERT INTO logs (drill_id, record_no, station, "
                    "counterpart, ts_micros, ts_iso, band, mode, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        drill_id, record_no, station, counterpart,
                        ts_micros, ts_iso, band, mode, _now_iso(),
                    ),
                )
                changed = True
            elif (
                row["station"], row["counterpart"], row["ts_micros"],
                row["band"], row["mode"],
            ) != (station, counterpart, ts_micros, band, mode):
                conn.execute(
                    "UPDATE logs SET station = ?, counterpart = ?, "
                    "ts_micros = ?, ts_iso = ?, band = ?, mode = ?, "
                    "updated_at = ? WHERE drill_id = ? AND record_no = ?",
                    (
                        station, counterpart, ts_micros, ts_iso,
                        band, mode, _now_iso(), drill_id, record_no,
                    ),
                )
                changed = True
            # 同内容重传：什么都不改，版本不推进。
            if changed:
                # 更正后若任一现存锁定失效：整次写入拒绝（事务回滚），
                # 状态与版本保持不变。
                latest = _load_records(conn, drill_id)
                _validate_existing_locks(conn, drill, latest)
                _recompute(conn, drill_id)
        return get_result(drill_id)
    finally:
        conn.close()


def batch_upsert_logs(
    drill_id: str, items: Iterable[dict]
) -> dict:
    """批量写入/更正：整体一个事务，任一非法则整批拒绝。

    批次内同一记录号出现多次视为非法（无法判定终态）。
    """
    items = list(items)
    seen: set[str] = set()
    for it in items:
        if it["record_no"] in seen:
            raise DuplicateRecordInBatchError(
                f"批次内记录号重复: {it['record_no']}"
            )
        seen.add(it["record_no"])

    conn = connect()
    try:
        changed = False
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            for it in items:
                record_no = it["record_no"]
                ts_micros = it["ts_micros"]
                ts_iso = _micros_to_iso(ts_micros)
                row = conn.execute(
                    "SELECT * FROM logs WHERE drill_id = ? AND record_no = ?",
                    (drill_id, record_no),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO logs (drill_id, record_no, station, "
                        "counterpart, ts_micros, ts_iso, band, mode, "
                        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            drill_id, record_no, it["station"],
                            it["counterpart"], ts_micros, ts_iso,
                            it["band"], it["mode"], _now_iso(),
                        ),
                    )
                    changed = True
                elif (
                    row["station"], row["counterpart"], row["ts_micros"],
                    row["band"], row["mode"],
                ) != (
                    it["station"], it["counterpart"], ts_micros,
                    it["band"], it["mode"],
                ):
                    conn.execute(
                        "UPDATE logs SET station = ?, counterpart = ?, "
                        "ts_micros = ?, ts_iso = ?, band = ?, mode = ?, "
                        "updated_at = ? WHERE drill_id = ? AND record_no = ?",
                        (
                            it["station"], it["counterpart"], ts_micros,
                            ts_iso, it["band"], it["mode"], _now_iso(),
                            drill_id, record_no,
                        ),
                    )
                    changed = True
            if changed:
                # 与单条写入一致：批量更正后任一现存锁定失效，则整批
                # 拒绝回滚（连同本批所有写入），状态与版本保持不变。
                latest = _load_records(conn, drill_id)
                _validate_existing_locks(conn, drill, latest)
                _recompute(conn, drill_id)
        return get_result(drill_id)
    finally:
        conn.close()


def seal_drill(drill_id: str) -> dict:
    conn = connect()
    try:
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            conn.execute(
                "UPDATE drills SET sealed = 1, sealed_at = ? WHERE drill_id = ?",
                (_now_iso(), drill_id),
            )
        return get_drill(drill_id)
    finally:
        conn.close()


def _normalize_lock_record_nos(
    record_a: str, record_b: str
) -> tuple[str, str]:
    a = (record_a or "").strip()
    b = (record_b or "").strip()
    if not a or not b:
        raise InvalidLockCandidateError("两个记录号均不能为空")
    # 与匹配结果一致：每对内部按记录号字典序升序存储。
    return (a, b) if a <= b else (b, a)


def lock_pair(drill_id: str, record_a: str, record_b: str) -> dict:
    """锁定一对互证候选。

    仅接受现存、相互指认、波段模式相同且时差不超限的两条日志，且
    两条记录不得已被其他锁定占用。重复锁定同一对视为重传：幂等
    成功、不推进版本。锁定后对剩余日志重算全场，锁定状态、结果与
    版本在同一事务原子提交。
    """
    a, b = _normalize_lock_record_nos(record_a, record_b)
    conn = connect()
    try:
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            existing = {
                key
                for key in _load_lock_keys(conn, drill_id)
            }
            if (a, b) in existing:
                # 重复锁定同一对：视为重传，无变化不推进版本。
                return get_result(drill_id)
            log_rows = conn.execute(
                "SELECT * FROM logs WHERE drill_id = ?", (drill_id,)
            ).fetchall()
            by_no = {r["record_no"]: r for r in log_rows}
            _check_lock_candidate(
                by_no, a, b, drill["allowed_diff_secs"]
            )
            # 两条记录均不得已被其他锁定占用。
            busy = sorted(
                {
                    no
                    for no in (a, b)
                    for (x, y) in existing
                    if no == x or no == y
                }
            )
            if busy:
                raise LockRecordBusyError(
                    "记录已被其他锁定占用: " + ", ".join(busy)
                )
            conn.execute(
                "INSERT INTO locks (drill_id, record_a, record_b, "
                "locked_at) VALUES (?, ?, ?, ?)",
                (drill_id, a, b, _now_iso()),
            )
            _recompute(conn, drill_id)
        return get_result(drill_id)
    finally:
        conn.close()


def unlock_pair(drill_id: str, record_a: str, record_b: str) -> dict:
    """解除已有锁定；解除不存在的锁定明确报错（404）。

    解除成功后对剩余日志重算全场；锁定状态、结果与版本原子提交。
    """
    a, b = _normalize_lock_record_nos(record_a, record_b)
    conn = connect()
    try:
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            row = conn.execute(
                "SELECT 1 FROM locks WHERE drill_id = ? AND record_a = ? "
                "AND record_b = ?",
                (drill_id, a, b),
            ).fetchone()
            if row is None:
                raise LockNotFoundError(
                    f"锁定不存在，无法解除: ({a}, {b})"
                )
            conn.execute(
                "DELETE FROM locks WHERE drill_id = ? AND record_a = ? "
                "AND record_b = ?",
                (drill_id, a, b),
            )
            _recompute(conn, drill_id)
        return get_result(drill_id)
    finally:
        conn.close()


def list_locks(drill_id: str) -> dict:
    """查询锁定清单及其最新时差（随日志更正保持一致，更正使锁定失效
    的写入会被拒绝，因此锁定对始终合法）。"""
    conn = connect()
    try:
        drill = _get_drill_row(conn, drill_id)
        rows = conn.execute(
            "SELECT * FROM locks WHERE drill_id = ? "
            "ORDER BY record_a, record_b",
            (drill_id,),
        ).fetchall()
        by_no = {
            r["record_no"]: r
            for r in conn.execute(
                "SELECT * FROM logs WHERE drill_id = ?", (drill_id,)
            ).fetchall()
        }
        items = []
        for row in rows:
            a_no, b_no = row["record_a"], row["record_b"]
            ra, rb = by_no.get(a_no), by_no.get(b_no)
            if ra is not None and rb is not None:
                diff = abs(ra["ts_micros"] - rb["ts_micros"]) / (
                    MICROS_PER_SECOND
                )
            else:
                diff = None  # 理论不可达：失效写入会被拒绝
            items.append(
                {
                    "record_a": a_no,
                    "record_b": b_no,
                    "time_diff_seconds": diff,
                    "locked_at": row["locked_at"],
                }
            )
        return {
            "drill_id": drill_id,
            "version": drill["version"],
            "sealed": bool(drill["sealed"]),
            "locks": items,
        }
    finally:
        conn.close()


def get_drill(drill_id: str) -> dict:
    conn = connect()
    try:
        row = _get_drill_row(conn, drill_id)
        return {
            "drill_id": row["drill_id"],
            "allowed_time_diff_seconds": row["allowed_diff_secs"],
            "version": row["version"],
            "sealed": bool(row["sealed"]),
            "created_at": row["created_at"],
            "sealed_at": row["sealed_at"],
        }
    finally:
        conn.close()


def list_drills() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT drill_id FROM drills ORDER BY drill_id"
        ).fetchall()
        return [get_drill(r["drill_id"]) for r in rows]
    finally:
        conn.close()


def list_logs(drill_id: str) -> list[dict]:
    conn = connect()
    try:
        _get_drill_row(conn, drill_id)  # 未知演练 -> 404
        rows = conn.execute(
            "SELECT * FROM logs WHERE drill_id = ? ORDER BY rowid",
            (drill_id,),
        ).fetchall()
        return [
            {
                "record_no": r["record_no"],
                "station": r["station"],
                "counterpart": r["counterpart"],
                "time": r["ts_iso"],
                "band": r["band"],
                "mode": r["mode"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_result(drill_id: str) -> dict:
    """查询当前（封存后即为最终）互证结果。"""
    conn = connect()
    try:
        drill = _get_drill_row(conn, drill_id)
        row = conn.execute(
            "SELECT * FROM results WHERE drill_id = ?", (drill_id,)
        ).fetchone()
        if row is None:  # 理论上不会发生（建演练即写空结果）
            return {
                "drill_id": drill_id,
                "version": drill["version"],
                "sealed": bool(drill["sealed"]),
                "pairs": [],
                "unpaired": [],
                "total_time_diff_seconds": 0.0,
            }
        return {
            "drill_id": drill_id,
            "version": row["version"],
            "sealed": bool(drill["sealed"]),
            "pairs": json.loads(row["pairs_json"]),
            "unpaired": json.loads(row["unpaired_json"]),
            "total_time_diff_seconds": row["total_time_diff_seconds"],
            "recomputed_at": row["recomputed_at"],
        }
    finally:
        conn.close()
