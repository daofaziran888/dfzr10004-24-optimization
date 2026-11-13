"""采集统计报表业务规则：状态流转、字段校验、分段生成与版本保留。

清单查询、登记和普通状态流转仍收在本模块；分段生成的重活在
``report_engine.py``。路由层只做参数解析，不在这里之外改报表状态。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.config import settings
from app.services.report_engine import (
    ACTIVE_JOB_STATES,
    DIAG_ARCHIVED,
    JOB_DONE,
    generation_engine,
)
from app.store import store

MODULE = "report"
REQUIRED_FIELDS = ["报表编号", "统计维度", "统计周期"]
STATUS_ORDER = ["草稿", "生成中", "已中断", "生成失败", "已生成", "已归档"]
# 归档只能发生在已生成（含可查看的历史版本）之后；删除只针对已归档
ACTION_RULES = {"归档报表": "已生成", "删除报表": "已归档"}
NEGATIVE_ACTIONS = []


class ReportService:
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
            rows = [row for row in rows if keyword in str(row.get("报表编号", ""))]
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
            "current_version": 0,
            "versions": [],
            "active_job_id": None,
            "保留策略": f"保留最近 {settings.report_version_keep} 个非归档版本，归档版本永久保留",
        }
        for optional in ("指标集合", "数据来源", "报表格式", "分发方式"):
            if values.get(optional) is not None:
                entry[optional] = values.get(optional)
        store.insert(MODULE, entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"统计报表 {entry_id} 不存在或已归档"

        if action == "生成报表":
            return self._trigger_generation(entry)

        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集统计报表可执行范围"

        if action == "归档报表":
            return self._archive(entry)

        # 删除报表：只允许删除已归档项
        target = ACTION_RULES[action]
        if entry.get("status") != target:
            return None, f"仅 {target} 状态的报表可执行{action}，当前为「{entry.get('status')}」"
        store.rows(MODULE).remove(entry)
        store.save()
        return entry, f"统计报表已{action}"

    def _trigger_generation(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        # 归档报表的任何重试都必须被挡住，历史归档不能被覆盖
        if entry.get("status") == "已归档":
            return None, "报表已归档，归档版本不可被重试覆盖"
        job, message, created = generation_engine.trigger(int(entry["id"]))
        if job is None:
            return None, message
        generation_engine.run_job(str(job["job_id"]))
        refreshed = store.find(MODULE, int(entry["id"]))
        return refreshed, message

    def _archive(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if entry.get("status") != "已生成":
            return None, f"仅已生成的报表可归档，当前为「{entry.get('status')}」"
        # 还有未完成执行时不允许归档，避免归档与生成互相覆盖
        active = generation_engine.list_jobs(int(entry["id"]))
        if any(job.get("state") in ACTIVE_JOB_STATES for job in active):
            return None, "报表仍有未完成的生成任务，请恢复或等待完成后再归档"
        entry["status"] = "已归档"
        entry["pending"] = False
        for version in entry.get("versions", []):
            version["archived"] = True
        entry["归档时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry["归档诊断码"] = DIAG_ARCHIVED
        store.save()
        return entry, "统计报表已归档，归档版本将被永久保留且不可重试覆盖"

    # ------------------------------------------------------------ 分段任务接口
    def list_jobs(self, report_id: int | None = None) -> list[dict[str, Any]]:
        return generation_engine.list_jobs(report_id)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return generation_engine.get_job(job_id)

    def resume_job(self, job_id: str) -> tuple[dict[str, Any] | None, str]:
        job, message = generation_engine.resume(job_id)
        if job is None:
            return None, message
        if job.get("state") == JOB_DONE:
            return job, message
        generation_engine.run_job(str(job["job_id"]))
        refreshed = generation_engine.get_job(str(job["job_id"]))
        return refreshed, message
