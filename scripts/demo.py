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
    ]
    show("批量写入 5 条日志", *call(
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

    show("封存演练", *call("POST", f"{base_url}/drills/{drill}/seal"))

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
