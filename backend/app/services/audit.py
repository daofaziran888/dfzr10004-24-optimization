"""采集审计日志业务规则：状态流转、待复核清单与报表结果同步。

报表生成完成后由 ``report_engine`` 调 ``create_from_report`` 把结果同步成
一条「待复核」审计记录，记录上带生成批次、重试次数和诊断码；本模块再提供
待复核清单查询与复核流转，构成采集审计台账。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "audit"
REQUIRED_FIELDS = ["记录编号", "操作类型", "采集计划"]
# 待复核是报表结果同步后的入口状态，复核后进入已复核，再可归档
STATUS_ORDER = ["待复核", "已记录", "已复核", "已归档"]
ACTION_RULES = {"标记复核": "已复核", "归档记录": "已复核"}
NEGATIVE_ACTIONS = []


class AuditService:
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
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def pending_review(
        self,
        *,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """采集审计台账的待复核清单：只看待复核记录，可按记录/报表编号检索。"""
        rows = [row for row in store.rows(MODULE) if row.get("status") == "待复核"]
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("记录编号", ""))
                or keyword in str(row.get("关联报表", ""))
            ]
        rows.sort(key=lambda row: str(row.get("执行时间", "")), reverse=True)
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
            "status": "已记录",
            "pending": True,
            "abnormal": False,
        }
        store.insert(MODULE, entry)
        return entry, []

    def create_from_report(self, audit_entry: dict[str, Any]) -> dict[str, Any]:
        """报表引擎同步入口：字段已由引擎组装好，这里只负责落盘。"""
        store.insert(MODULE, audit_entry)
        return audit_entry

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"审计记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集审计日志可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "标记复核":
            # 待复核与已记录都可以复核；已复核/已归档的记录不允许重复操作
            if entry.get("status") not in ("待复核", "已记录"):
                return None, f"当前状态「{entry.get('status')}」无需再次复核"
            entry["复核状态"] = "已复核"
        elif action == "归档记录" and entry.get("status") != "已复核":
            return None, f"仅已复核的记录可归档，当前为「{entry.get('status')}」"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save()
        return entry, f"审计记录已{action}"
