"""采集明细数据源：报表分段计算的输入。

报表本身不保存采集明细，只保存按「统计周期 + 报表编号」聚合后的结果段。
这个模块模拟采集链路沉淀下来的明细表，并提供两件关键能力：

1. ``snapshot``：报表触发生成时冻结一份数据快照，之后即使有新采集数据
   到达，正在生成的报表也只按快照计算，不会被悄悄改写；
2. ``inject_fault``：测试和排障用的故障注入，让指定生成段在计算时抛错，
   用来验证中断恢复、诊断码和重试次数的持久化。

数据源同样落盘（``datasource.json``），保证服务重启后待聚合明细还在。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from copy import deepcopy
from typing import Any

from app.config import settings

_FILENAME = "datasource.json"

# 预置的统计周期与报表编号组合，首次启动时据此生成示例明细
SEED_PERIODS = ["2026-W36", "2026-W37", "2026-W38"]
SEED_REPORT_CODES = ["REPO-0001", "REPO-0002", "REPO-0003"]
# 每个组合下放两条明细，保证分段结果有内容、段之间可独立校验
_ROWS_PER_SEGMENT = 2


class DataSource:
    def __init__(self, data_dir: str | None = None) -> None:
        self._lock = threading.RLock()
        self._data_dir = data_dir or settings.data_dir
        self._path = os.path.join(self._data_dir, _FILENAME)
        # 故障注入只在当前进程生效，不落盘：重启后故障自然清除，避免卡死
        self._faults: set[tuple[str, str]] = set()
        self._rows: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                rows = payload.get("rows")
                if isinstance(rows, list):
                    self._rows = [dict(row) for row in rows]
                    return
            except (json.JSONDecodeError, OSError):
                pass
        self._rows = []
        seq = 1
        for period in SEED_PERIODS:
            for code in SEED_REPORT_CODES:
                for offset in range(_ROWS_PER_SEGMENT):
                    self._rows.append({
                        "row_id": seq,
                        "统计周期": period,
                        "报表编号": code,
                        "采集计划": f"TASK-000{offset + 1}",
                        "采集量": 100 + seq * 7,
                        "异常量": seq % 3,
                    })
                    seq += 1
        self._flush()

    def _flush(self) -> None:
        os.makedirs(self._data_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".ds-", suffix=".tmp", dir=self._data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "rows": self._rows}, handle,
                          ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    # ------------------------------------------------------------------ 数据写入
    def ingest(self, period: str, report_code: str, plan: str,
               collected: int, abnormal: int) -> dict[str, Any]:
        """新采集数据到达：追加明细。已经冻结的快照不受影响。"""
        with self._lock:
            row_id = max((int(row["row_id"]) for row in self._rows), default=0) + 1
            row = {
                "row_id": row_id,
                "统计周期": period,
                "报表编号": report_code,
                "采集计划": plan,
                "采集量": int(collected),
                "异常量": int(abnormal),
            }
            self._rows.append(row)
            self._flush()
            return dict(row)

    def snapshot(self) -> dict[str, Any]:
        """冻结当前全量明细的深拷贝，作为一次报表生成的唯一计算口径。"""
        with self._lock:
            return {"rows": deepcopy(self._rows), "row_count": len(self._rows)}

    # ------------------------------------------------------------------ 分段计算
    def list_segments(self, snapshot: dict[str, Any]) -> list[dict[str, str]]:
        """列出快照中全部（统计周期, 报表编号）组合，即全部待生成段。"""
        combos: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for row in snapshot.get("rows", []):
            key = (str(row["统计周期"]), str(row["报表编号"]))
            if key not in seen:
                seen.add(key)
                combos.append(key)
        combos.sort()
        return [{"统计周期": period, "报表编号": code} for period, code in combos]

    def compute_segment(self, snapshot: dict[str, Any], period: str,
                        report_code: str) -> dict[str, Any]:
        """按冻结快照计算单个生成段；命中故障注入时抛 ``SegmentFault``。"""
        if (period, report_code) in self._faults:
            raise SegmentFault(period, report_code)
        matched = [
            row for row in snapshot.get("rows", [])
            if str(row.get("统计周期")) == period and str(row.get("报表编号")) == report_code
        ]
        if not matched:
            raise SegmentFault(period, report_code, reason="快照中没有该区间的采集明细")
        plans = sorted({str(row.get("采集计划", "")) for row in matched if row.get("采集计划")})
        return {
            "统计周期": period,
            "报表编号": report_code,
            "明细行数": len(matched),
            "采集量合计": sum(int(row.get("采集量", 0)) for row in matched),
            "异常量合计": sum(int(row.get("异常量", 0)) for row in matched),
            "来源计划": plans,
        }

    def inject_fault(self, period: str, report_code: str) -> None:
        self._faults.add((period, report_code))

    def clear_faults(self) -> None:
        self._faults.clear()


class SegmentFault(RuntimeError):
    """单个生成段计算失败，携带诊断信息用于持久化诊断码。"""

    def __init__(self, period: str, report_code: str, *, reason: str = "故障注入触发计算失败") -> None:
        super().__init__(f"生成段 {period}/{report_code} 计算失败：{reason}")
        self.period = period
        self.report_code = report_code
        self.reason = reason


data_source = DataSource()
