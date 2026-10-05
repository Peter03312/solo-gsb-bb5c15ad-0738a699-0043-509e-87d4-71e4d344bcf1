#!/usr/bin/env python3
"""一键演示脚本：创建演练 -> 写入日志 -> 查询互证 -> 更正 -> 封存。

用法：
    python scripts/demo.py [BASE_URL]

默认 BASE_URL=http://127.0.0.1:8080。
仅使用标准库，无需安装额外依赖。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def call(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} [{status}] ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main(base_url: str) -> None:
    # 带时间戳，保证演示可重复运行
    from datetime import datetime, timezone
    drill = "DEMO-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    show("创建演练（允许时差 10 秒）", *call(
        "POST", f"{base_url}/drills",
        {"drill_id": drill, "allowed_time_diff_seconds": 10},
    ))

    logs = [
        # BH1AAA 与 BH2BBB 互指，时差 5 秒，同波段同模式 -> 可配对
        {"record_no": "R1", "station": "BH1AAA", "counterpart": "BH2BBB",
         "time": "2026-10-04T08:00:00+08:00", "band": "40m", "mode": "SSB"},
        {"record_no": "R2", "station": "BH2BBB", "counterpart": "BH1AAA",
         "time": "2026-10-04T00:00:05+00:00", "band": "40m", "mode": "SSB"},
        # 单边记录，无人互指 -> 未配对
        {"record_no": "R3", "station": "BH3CCC", "counterpart": "BH4DDD",
         "time": "2026-10-04T00:00:00+00:00", "band": "40m", "mode": "SSB"},
        # 与 R1/R2 模式不同 -> 不可配对
        {"record_no": "R4", "station": "BH1AAA", "counterpart": "BH2BBB",
         "time": "2026-10-04T00:00:01+00:00", "band": "40m", "mode": "CW"},
        {"record_no": "R5", "station": "BH2BBB", "counterpart": "BH1AAA",
         "time": "2026-10-04T00:00:01+00:00", "band": "40m", "mode": "CW"},
        # 备份记录：R7 为 CW 单边记录；R8 与 R3 同呼号对/同 SSB，
        # (R3,R8) 候选本身合法，但锁定 (R3,R6) 后 R3 被占用，用于演示
        # 409 lock_record_busy。
        {"record_no": "R7", "station": "BH3CCC", "counterpart": "BH4DDD",
         "time": "2026-10-04T00:00:00+00:00", "band": "40m", "mode": "CW"},
        {"record_no": "R8", "station": "BH4DDD", "counterpart": "BH3CCC",
         "time": "2026-10-04T00:00:02+00:00", "band": "40m", "mode": "SSB"},
    ]
    show("批量写入 7 条日志", *call(
        "POST", f"{base_url}/drills/{drill}/logs", {"logs": logs}
    ))

    # 同内容重传（规范化后一致）-> 版本不变
    show("R1 同内容重传（版本应不变）", *call(
        "PUT", f"{base_url}/drills/{drill}/logs/R1",
        {**logs[0], "station": " bh1aaa "},
    ))

    # 异内容更正：把 R3 的对方记录 R6 补上，但时差 60 秒超过 10 秒 -> 仍不配对
    show("补一条超出时差的互指记录 R6", *call(
        "PUT", f"{base_url}/drills/{drill}/logs/R6",
        {"record_no": "R6", "station": "BH4DDD", "counterpart": "BH3CCC",
         "time": "2026-10-04T00:01:00+00:00", "band": "40m", "mode": "SSB"},
    ))

    # 更正 R6 到 8 秒差 -> R3-R6 配对，全场重算
    show("更正 R6（时差变为 8 秒，全场重算）", *call(
        "PUT", f"{base_url}/drills/{drill}/logs/R6",
        {"record_no": "R6", "station": "BH4DDD", "counterpart": "BH3CCC",
         "time": "2026-10-04T00:00:08+00:00", "band": "40m", "mode": "SSB"},
    ))

    show("查询最终互证结果", *call("GET", f"{base_url}/drills/{drill}/result"))

    # --- 复核员锁定：强制保留 (R3,R6)，对剩余日志重算全场 ---
    show("锁定候选对 (R3,R6)", *call(
        "PUT", f"{base_url}/drills/{drill}/locks/R3/R6",
    ))
    show("重复锁定（反序重传，版本应不变）", *call(
        "PUT", f"{base_url}/drills/{drill}/locks/R6/R3",
    ))
    show("查询锁定清单", *call("GET", f"{base_url}/drills/{drill}/locks"))
    # 占用冲突：(R3,R8) 本身是合法候选（互指、同波段同模式、时差 8s），
    # 但 R3 已被锁定 (R3,R6) 占用 -> 409 lock_record_busy
    show("再次锁定占用 R3 的合法对（应 409 lock_record_busy）", *call(
        "PUT", f"{base_url}/drills/{drill}/locks/R3/R8",
    ))
    # 非法候选：R99X 不存在
    show("锁定含不存在记录的对（应 422 invalid_lock_candidate）", *call(
        "PUT", f"{base_url}/drills/{drill}/locks/R3/R99X",
    ))
    # 更正使锁定失效：把 R6 改到 10 分钟后 -> 整次写入拒绝
    show("更正 R6 使锁定失效（应 409，状态不变）", *call(
        "PUT", f"{base_url}/drills/{drill}/logs/R6",
        {"record_no": "R6", "station": "BH4DDD", "counterpart": "BH3CCC",
         "time": "2026-10-04T00:10:08+00:00", "band": "40m", "mode": "SSB"},
    ))
    show("解除不存在的锁定（应 404 lock_not_found）", *call(
        "DELETE", f"{base_url}/drills/{drill}/locks/R1/R6",
    ))
    show("解除锁定 (R3,R6)（重算全场）", *call(
        "DELETE", f"{base_url}/drills/{drill}/locks/R6/R3",
    ))

    show("封存演练", *call("POST", f"{base_url}/drills/{drill}/seal"))

    show("封存后锁定（应被拒绝 409）", *call(
        "PUT", f"{base_url}/drills/{drill}/locks/R1/R2",
    ))
    show("封存后锁定清单仍可查询", *call(
        "GET", f"{base_url}/drills/{drill}/locks",
    ))

    status, body = call(
        "PUT", f"{base_url}/drills/{drill}/logs/R7",
        {"record_no": "R7", "station": "BH5EEE", "counterpart": "BH6FFF",
         "time": "2026-10-04T00:00:00+00:00", "band": "40m", "mode": "SSB"},
    )
    show("封存后写入（应被拒绝 409）", status, body)

    show("封存后仍可查询结果", *call("GET", f"{base_url}/drills/{drill}/result"))


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
    main(base.rstrip("/"))
