"""采集任务编排业务规则：状态流转、字段校验与来源说明保留。

采集计划是采集明细与报表审计的上游来源，因此计划清单必须保留「来源说明」，
审计台账同步报表结果时也能回溯到具体计划。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "taskplan"
REQUIRED_FIELDS = ["计划编号", "目标站点", "采集频率"]
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已完成"]
ACTION_RULES = {"启动计划": "草稿", "暂停计划": "运行中", "归档计划": "已暂停"}
NEGATIVE_ACTIONS = []


class TaskplanService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("计划编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        entry = {
            "id": store.next_id(MODULE),
            **{field: values.get(field) for field in REQUIRED_FIELDS},
            "status": STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
            # 来源说明随计划持久化，登记时可带，缺省给可追溯的默认口径
            "来源说明": str(values.get("来源说明") or "人工登记的采集计划，作为采集明细与审计台账的来源依据"),
        }
        store.insert(MODULE, entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采集计划 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集任务编排可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save()
        return entry, f"采集计划已{action}"
