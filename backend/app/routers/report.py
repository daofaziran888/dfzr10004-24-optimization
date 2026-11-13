"""采集统计报表接口：维护统计报表，覆盖分段生成、断点恢复、归档与版本查看。

动作接口仍走 ``/{entry_id}/actions`` 保持调用链不变；分段生成的任务、
生成段和快照通过独立的 jobs 接口暴露，前端轮询这些接口即可在刷新后
还原上次位置。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.report import ReportService
from app.services.report_engine import generation_engine

router = APIRouter(prefix="/api/report", tags=["采集统计报表"])

service = ReportService()

LIST_FIELDS = ["报表编号", "统计维度", "统计周期", "指标集合", "数据来源", "生成时间", "报表格式", "分发方式"]
STATUSES = ["草稿", "生成中", "已中断", "生成失败", "已生成", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按报表编号检索"),
    status: str | None = Query(default=None, description="草稿、生成中、已中断、生成失败、已生成、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按报表编号与状态过滤采集统计报表列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/jobs", response_model=dict)
def list_jobs(report_id: int | None = Query(default=None, description="可选：只看某张报表的生成任务")) -> dict[str, Any]:
    """生成任务清单：含生成段、当前批次、诊断码与重试次数，全部来自持久化数据。"""
    jobs = service.list_jobs(report_id)
    return {
        "total": len(jobs),
        "items": [generation_engine.job_summary(job) for job in jobs],
    }


@router.get("/jobs/{job_id}", response_model=dict)
def get_job(job_id: str) -> dict[str, Any]:
    """读取单个生成任务的完整明细（含每段结果与快照口径），刷新页面后据此还原。"""
    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"生成任务 {job_id} 不存在")
    payload = generation_engine.job_summary(job)
    payload["segments"] = job.get("segments", [])
    payload["snapshot"] = {
        "row_count": job.get("snapshot", {}).get("row_count", 0),
    }
    return payload


@router.post("/jobs/{job_id}/resume", response_model=ActionResult)
def resume_job(job_id: str) -> ActionResult:
    """从失败段/中断处恢复生成：已完成段不重算，只补未完成区间。"""
    job, message = service.resume_job(job_id)
    if job is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=generation_engine.job_summary(job))


@router.post("/debug/faults", response_model=ActionResult)
def inject_fault(payload: EntryPayload) -> ActionResult:
    """排障/演练入口：给指定生成段注入或清除计算故障，验证中断恢复与诊断码。

    故障只在当前进程内存生效、不落盘，服务重启后自动清除，避免永久卡死。
    """
    from app.services.datasource import data_source

    values = payload.values
    if values.get("clear"):
        data_source.clear_faults()
        return ActionResult(ok=True, message="已清除全部故障注入")
    period = str(values.get("统计周期") or "").strip()
    code = str(values.get("报表编号") or "").strip()
    if not period or not code:
        return ActionResult(ok=False, message="故障注入需要提供统计周期与报表编号")
    data_source.inject_fault(period, code)
    return ActionResult(ok=True, message=f"已对生成段 {period}/{code} 注入计算故障")


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条统计报表明细（含历史版本与生成段摘要）；不存在时给出可读错误。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"统计报表 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条统计报表，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="统计报表已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条统计报表执行生成报表、归档报表、删除报表；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集统计报表清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "report", "total": total, "items": items}
