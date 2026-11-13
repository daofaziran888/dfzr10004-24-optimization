"""采集统计报表的分段生成引擎。

调用链定位：``routers/report.py`` → ``services/report.py``（清单与状态流转）
→ 本模块（分段生成、断点恢复、快照与版本保留）→ ``services/datasource.py``
（按冻结快照计算）→ ``services/audit.py``（结果同步待复核清单）。

设计要点（对应题目要求）：

- **分段**：按「统计周期 + 报表编号」把一次生成拆成多个生成段，段是计算、
  重试与恢复的最小单位；
- **批次**：每 ``REPORT_BATCH_SIZE`` 个段组成一个批次，批次内最多
  ``REPORT_CONCURRENCY`` 个段并发计算，批次大小与并发上限在 ``config.py``；
- **持久化**：生成段状态、当前批次、重试次数、诊断码和开始时的快照都写入
  ``report_jobs`` 表并随 Store 落盘，中断重启后位置不丢；
- **恢复**：再次触发时只恢复同一个未完成任务，已完成段直接跳过，从失败段
  继续，绝不重复计算已完成区间；
- **单有效执行**：同一报表同时只允许一个有效执行，重复触发返回既有任务；
- **快照隔离**：任务开始即冻结数据源快照，新到达的数据不会改写正在生成
  的报表，冲突时以开始时的快照为准；
- **版本保留**：完成后生成新版本，历史已生成版本在完成前及保留期内都可
  查看，超出保留数量的非归档旧版本才清理；归档报表拒绝任何重试覆盖。
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from app.config import settings
from app.services.datasource import SegmentFault, data_source
from app.store import store

MODULE = "report"
JOB_MODULE = "report_jobs"

# 任务与生成段状态
JOB_RUNNING = "生成中"
JOB_INTERRUPTED = "已中断"
JOB_FAILED = "生成失败"
JOB_DONE = "已生成"

SEG_PENDING = "待计算"
SEG_RUNNING = "计算中"
SEG_DONE = "已完成"
SEG_FAILED = "失败"

# 诊断码：持久化在任务和生成段上，刷新页面也能看到失败原因
DIAG_NONE = ""
DIAG_INTERRUPTED = "E_INTERRUPTED"          # 进程退出/重启导致的中断
DIAG_SEGMENT_FAILED = "E_SEGMENT_FAILED"    # 单段重试用尽仍失败
DIAG_ARCHIVED = "E_ARCHIVED"                # 报表已归档，拒绝重试覆盖
DIAG_BUSY = "E_BUSY"                        # 已有有效执行在跑
DIAG_SNAPSHOT_DRIFT = "E_SNAPSHOT_DRIFT"    # 预留：快照口径冲突说明

ACTIVE_JOB_STATES = (JOB_RUNNING, JOB_INTERRUPTED, JOB_FAILED)

# 进程内串行锁：保证「检查有效执行 → 创建/恢复任务 → 执行」不会并发穿插
_engine_lock = threading.RLock()
# 记录进程内真正在跑的任务 id，重复触发生成时直接复用
_running_job_ids: set[str] = set()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _job_table() -> list[dict[str, Any]]:
    return store.rows(JOB_MODULE)


def _find_job(job_id: str) -> dict[str, Any] | None:
    for job in _job_table():
        if job.get("job_id") == job_id:
            return job
    return None


def _active_job(report_id: int) -> dict[str, Any] | None:
    for job in _job_table():
        if int(job.get("report_id", 0)) == report_id and job.get("state") in ACTIVE_JOB_STATES:
            return job
    return None


def reconcile_interrupted_jobs() -> int:
    """服务重启时调用：把停在「生成中」的任务标记为已中断。

    进程退出意味着内存里的执行线程已经没了，但落盘状态可能还停在生成中。
    这里统一改成「已中断」并写入诊断码，前端重新打开时看到的是可恢复的
    真实状态，而不是永远转圈的生成中。返回被纠正的任务数。
    """
    fixed = 0
    with _engine_lock:
        for job in _job_table():
            if job.get("state") != JOB_RUNNING:
                continue
            job["state"] = JOB_INTERRUPTED
            job["diagnostic_code"] = DIAG_INTERRUPTED
            job["updated_at"] = _now()
            for seg in job.get("segments", []):
                if seg.get("state") == SEG_RUNNING:
                    seg["state"] = SEG_FAILED
                    seg["diagnostic_code"] = DIAG_INTERRUPTED
                    seg["error_message"] = "服务重启，生成中断，可从该段恢复"
            entry = store.find(MODULE, int(job["report_id"]))
            if entry is not None and entry.get("status") in (JOB_RUNNING, JOB_FAILED):
                entry["status"] = JOB_INTERRUPTED
                entry["pending"] = True
            fixed += 1
        if fixed:
            store.save()
    return fixed


class ReportGenerationEngine:
    # ------------------------------------------------------------ 查询与组装
    def list_jobs(self, report_id: int | None = None) -> list[dict[str, Any]]:
        jobs = _job_table()
        if report_id is not None:
            jobs = [job for job in jobs if int(job.get("report_id", 0)) == report_id]
        return sorted(jobs, key=lambda job: str(job.get("created_at", "")), reverse=True)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return _find_job(job_id)

    def job_summary(self, job: dict[str, Any]) -> dict[str, Any]:
        """给前端的任务视图：进度、当前批次、诊断码与重试次数一眼可见。"""
        segments = job.get("segments", [])
        total = len(segments)
        done = sum(1 for seg in segments if seg.get("state") == SEG_DONE)
        failed = sum(1 for seg in segments if seg.get("state") == SEG_FAILED)
        retries = sum(int(seg.get("retries", 0)) for seg in segments)
        return {
            "job_id": job.get("job_id"),
            "report_id": job.get("report_id"),
            "state": job.get("state"),
            "diagnostic_code": job.get("diagnostic_code", DIAG_NONE),
            "error_message": job.get("error_message", ""),
            "total_segments": total,
            "done_segments": done,
            "failed_segments": failed,
            "current_batch": job.get("current_batch", 0),
            "total_batches": job.get("total_batches", 0),
            "retry_count": retries,
            "snapshot_row_count": job.get("snapshot", {}).get("row_count", 0),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
        }

    # ------------------------------------------------------------ 触发与恢复
    def trigger(self, report_id: int) -> tuple[dict[str, Any] | None, str, bool]:
        """触发报表生成；返回 ``(任务, 提示信息, 是否新建任务)``。

        同一报表只保留一个有效执行：存在未完成任务时一律复用它（恢复），
        已归档报表直接拒绝，避免历史归档被重试覆盖。
        """
        entry = store.find(MODULE, report_id)
        if entry is None:
            return None, f"统计报表 {report_id} 不存在", False
        if entry.get("status") == "已归档":
            return None, "报表已归档，归档版本不可被重试覆盖", False

        with _engine_lock:
            existing = _active_job(report_id)
            if existing is not None:
                if existing.get("state") == JOB_RUNNING and existing["job_id"] in _running_job_ids:
                    return existing, "报表已有有效执行正在生成，已复用该执行", False
                # 中断/失败任务：从失败段恢复，不重新建任务
                return existing, "检测到未完成的生成任务，将从失败段恢复", False

            job = self._create_job(entry)
            store.save()
            return job, "生成任务已建立，开始分段生成", True

    def _create_job(self, entry: dict[str, Any]) -> dict[str, Any]:
        snapshot = data_source.snapshot()
        segment_specs = data_source.list_segments(snapshot)
        batch_size = max(settings.report_batch_size, 1)
        segments: list[dict[str, Any]] = []
        for index, spec in enumerate(segment_specs):
            segments.append({
                "segment_id": f"SEG-{index + 1:03d}",
                "统计周期": spec["统计周期"],
                "报表编号": spec["报表编号"],
                "batch_index": index // batch_size + 1,
                "state": SEG_PENDING,
                "retries": 0,
                "diagnostic_code": DIAG_NONE,
                "error_message": "",
                "result": None,
            })
        total_batches = max((seg["batch_index"] for seg in segments), default=0)
        job = {
            "job_id": f"JOB-{uuid.uuid4().hex[:12]}",
            "report_id": int(entry["id"]),
            "报表编号": entry.get("报表编号"),
            "state": JOB_RUNNING,
            "current_batch": 1 if segments else 0,
            "total_batches": total_batches,
            "diagnostic_code": DIAG_NONE,
            "error_message": "",
            "snapshot": snapshot,
            "segments": segments,
            "created_at": _now(),
            "updated_at": _now(),
        }
        entry["status"] = JOB_RUNNING
        entry["pending"] = True
        entry["active_job_id"] = job["job_id"]
        store.rows(JOB_MODULE)  # 确保表已初始化
        _job_table().append(job)
        return job

    def resume(self, job_id: str) -> tuple[dict[str, Any] | None, str]:
        """显式恢复一个中断/失败的任务；已完成或在跑的任务原样返回。"""
        with _engine_lock:
            job = _find_job(job_id)
            if job is None:
                return None, f"生成任务 {job_id} 不存在"
            if job.get("state") == JOB_DONE:
                return job, "该任务已生成完成，无需恢复"
            if job.get("state") == JOB_RUNNING and job_id in _running_job_ids:
                return job, "任务正在生成中，请勿重复触发"
            entry = store.find(MODULE, int(job["report_id"]))
            if entry is not None and entry.get("status") == "已归档":
                job["diagnostic_code"] = DIAG_ARCHIVED
                store.save()
                return None, "报表已归档，归档版本不可被重试覆盖"
            return job, "任务已置为恢复执行，从首个未完成批次继续"

    # ------------------------------------------------------------ 执行引擎
    def run_job(self, job_id: str) -> dict[str, Any]:
        """执行（或恢复执行）一个生成任务，幂等：已完成段不会被重复计算。"""
        with _engine_lock:
            job = _find_job(job_id)
            if job is None:
                raise ValueError(f"生成任务 {job_id} 不存在")
            if job.get("state") == JOB_DONE:
                return job
            # 同一进程内已有执行在跑：直接复用，绝不开第二个执行重复计算
            if job_id in _running_job_ids:
                return job
            entry = store.find(MODULE, int(job["report_id"]))
            if entry is not None and entry.get("status") == "已归档":
                job["state"] = JOB_FAILED
                job["diagnostic_code"] = DIAG_ARCHIVED
                job["error_message"] = "报表已归档，拒绝继续生成"
                job["updated_at"] = _now()
                store.save()
                return job
            job["state"] = JOB_RUNNING
            job["diagnostic_code"] = DIAG_NONE
            job["error_message"] = ""
            if entry is not None:
                entry["status"] = JOB_RUNNING
                entry["active_job_id"] = job_id
            _running_job_ids.add(job_id)
        try:
            self._run_batches(job)
        finally:
            _running_job_ids.discard(job_id)
            with _engine_lock:
                store.save()
        with _engine_lock:
            if job["state"] == JOB_RUNNING:
                self._complete_job(job)
            store.save()
        return job

    def _run_batches(self, job: dict[str, Any]) -> None:
        snapshot = job["snapshot"]
        max_retries = max(settings.segment_max_retries, 1)
        for batch_index in range(1, int(job["total_batches"]) + 1):
            with _engine_lock:
                # 恢复路径：跳过全部完成的批次，current_batch 定位到首个未完成批
                batch_segments = [
                    seg for seg in job["segments"] if seg["batch_index"] == batch_index
                ]
                if all(seg["state"] == SEG_DONE for seg in batch_segments):
                    job["current_batch"] = batch_index + 1 if batch_index < job["total_batches"] else batch_index
                    store.save()
                    continue
                job["current_batch"] = batch_index
                candidates = [
                    seg for seg in batch_segments if seg["state"] != SEG_DONE
                ]
                store.save()

            failures = self._run_batch(snapshot, candidates, max_retries)

            with _engine_lock:
                store.save()
                if failures:
                    # 批次内有段重试用尽：任务停在该批次，下次从这里恢复
                    job["state"] = JOB_FAILED
                    job["diagnostic_code"] = DIAG_SEGMENT_FAILED
                    names = "、".join(
                        f"{seg['统计周期']}/{seg['报表编号']}" for seg in failures
                    )
                    job["error_message"] = f"批次 {batch_index} 存在失败段：{names}"
                    entry = store.find(MODULE, int(job["report_id"]))
                    if entry is not None:
                        entry["status"] = JOB_FAILED
                        entry["pending"] = True
                    return

    def _run_batch(
        self,
        snapshot: dict[str, Any],
        segments: list[dict[str, Any]],
        max_retries: int,
    ) -> list[dict[str, Any]]:
        """执行一个批次：并发上限内并行计算，单段失败按上限重试并落盘。"""
        failures: list[dict[str, Any]] = []
        concurrency = max(min(settings.report_concurrency, len(segments)), 1)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            future_map = {
                pool.submit(self._run_segment, snapshot, seg, max_retries): seg
                for seg in segments
            }
            for future in future_map:
                seg = future_map[future]
                ok = future.result()
                if not ok:
                    failures.append(seg)
        return failures

    def _run_segment(
        self,
        snapshot: dict[str, Any],
        seg: dict[str, Any],
        max_retries: int,
    ) -> bool:
        """计算单个生成段，含重试；每个状态变化都立刻持久化。"""
        # 已完成段直接跳过——恢复时绝不重复计算已完成区间
        if seg.get("state") == SEG_DONE:
            return True
        while True:
            with _engine_lock:
                seg["state"] = SEG_RUNNING
                seg["diagnostic_code"] = DIAG_NONE
                seg["error_message"] = ""
                store.save()
            try:
                result = data_source.compute_segment(
                    snapshot, str(seg["统计周期"]), str(seg["报表编号"])
                )
            except SegmentFault as exc:
                with _engine_lock:
                    seg["retries"] = int(seg.get("retries", 0)) + 1
                    if seg["retries"] > max_retries:
                        seg["state"] = SEG_FAILED
                        seg["diagnostic_code"] = DIAG_SEGMENT_FAILED
                        seg["error_message"] = exc.reason
                        store.save()
                        return False
                    # 还在重试额度内：落盘重试次数后继续下一轮
                    seg["state"] = SEG_PENDING
                    seg["error_message"] = f"第 {seg['retries']} 次尝试失败：{exc.reason}"
                    store.save()
                continue
            with _engine_lock:
                seg["state"] = SEG_DONE
                seg["diagnostic_code"] = DIAG_NONE
                seg["error_message"] = ""
                seg["result"] = result
                store.save()
            return True

    # ------------------------------------------------------------ 完成与版本
    def _complete_job(self, job: dict[str, Any]) -> None:
        entry = store.find(MODULE, int(job["report_id"]))
        results = [
            seg["result"] for seg in job["segments"] if seg.get("result")
        ]
        total_collected = sum(int(item.get("采集量合计", 0)) for item in results)
        total_abnormal = sum(int(item.get("异常量合计", 0)) for item in results)
        plan_set = sorted({
            plan for item in results for plan in item.get("来源计划", [])
        })
        # 版本号取自报表条目上持久化的 current_version，保证重复生成连续递增
        version_no = int(entry.get("current_version", 0) or 0) + 1 if entry else 1
        completed_at = _now()
        segment_summaries = [
            {
                "segment_id": seg["segment_id"],
                "统计周期": seg["统计周期"],
                "报表编号": seg["报表编号"],
                "批次": seg["batch_index"],
                "重试次数": seg["retries"],
                "诊断码": seg["diagnostic_code"],
                "结果": seg["result"],
            }
            for seg in job["segments"]
        ]
        version_record = {
            "version_no": version_no,
            "job_id": job["job_id"],
            "generated_at": completed_at,
            "snapshot_row_count": job["snapshot"].get("row_count", 0),
            "segment_count": len(job["segments"]),
            "retry_count": sum(int(seg["retries"]) for seg in job["segments"]),
            "summary": {
                "采集量合计": total_collected,
                "异常量合计": total_abnormal,
                "来源计划": plan_set,
                "生成段数": len(results),
            },
            "segments": segment_summaries,
            "archived": False,
        }
        if entry is not None:
            entry.setdefault("versions", []).append(version_record)
            entry["status"] = JOB_DONE
            entry["pending"] = False
            entry["abnormal"] = False
            entry["active_job_id"] = None
            entry["current_version"] = version_no
            entry["最新快照行数"] = job["snapshot"].get("row_count", 0)
            entry["生成时间"] = completed_at
            entry["生成批次"] = job["total_batches"]
            entry["累计重试"] = version_record["retry_count"]
            entry["摘要"] = version_record["summary"]
            self._prune_versions(entry)
        job["state"] = JOB_DONE
        job["current_batch"] = job["total_batches"]
        job["diagnostic_code"] = DIAG_NONE
        job["error_message"] = ""
        job["updated_at"] = completed_at
        job["completed_at"] = completed_at

        # 结果同步到采集审计台账（待复核清单），失败段也会带诊断码进入清单
        self._sync_audit(job, entry, version_record)

    def _prune_versions(self, entry: dict[str, Any]) -> None:
        """保留当前版本 + 最近 ``keep-1`` 个非归档历史版本；归档版本永不清理。"""
        keep = max(settings.report_version_keep, 1)
        versions = entry.setdefault("versions", [])
        current = entry.get("current_version")
        candidates = [
            item for item in versions
            if not item.get("archived") and item.get("version_no") != current
        ]
        candidates.sort(key=lambda item: int(item.get("version_no", 0)), reverse=True)
        # 当前版本固定占一个保留名额，历史版本最多再留 keep-1 个
        drop_ids = {item["version_no"] for item in candidates[keep - 1:]}
        if drop_ids:
            entry["versions"] = [
                item for item in versions if item.get("version_no") not in drop_ids
            ]

    def _sync_audit(
        self,
        job: dict[str, Any],
        entry: dict[str, Any] | None,
        version_record: dict[str, Any],
    ) -> None:
        """把本次生成结果同步成一条待复核审计记录。

        同一任务重复完成（理论上不会，防御性处理）按 job_id 去重，保证
        「同步审计清单」不会因为重试产生重复台账。
        """
        from app.services.audit import AuditService

        audit_rows = store.rows("audit")
        for row in audit_rows:
            if row.get("关联任务号") == job["job_id"]:
                return
        failed_segments = [
            seg for seg in job["segments"] if seg.get("state") != SEG_DONE
        ]
        service = AuditService()
        record_id = store.next_id("audit")
        audit_entry = {
            "id": record_id,
            "记录编号": f"AUDI-R{record_id:04d}",
            "操作类型": "报表生成复核",
            "采集计划": "、".join(version_record["summary"].get("来源计划", [])) or "未关联采集计划",
            "执行人": "系统生成引擎",
            "执行时间": version_record["generated_at"],
            "结果摘要": (
                f"报表 {entry.get('报表编号') if entry else job.get('报表编号')} "
                f"V{version_record['version_no']} 生成完成，"
                f"批次 {job['total_batches']} 个、生成段 {len(job['segments'])} 个、"
                f"重试 {version_record['retry_count']} 次"
            ),
            "变更内容": (
                f"快照行数 {version_record['snapshot_row_count']}，"
                f"采集量合计 {version_record['summary'].get('采集量合计')}，"
                f"异常量合计 {version_record['summary'].get('异常量合计')}"
            ),
            "复核状态": "待复核",
            "status": "待复核",
            "pending": True,
            "abnormal": bool(failed_segments),
            "关联报表": entry.get("报表编号") if entry else job.get("报表编号"),
            "关联任务号": job["job_id"],
            "生成批次": job["total_batches"],
            "重试次数": version_record["retry_count"],
            "诊断码": job.get("diagnostic_code", DIAG_NONE),
            "来源说明": f"报表生成任务 {job['job_id']} 自动同步",
        }
        service.create_from_report(audit_entry)


generation_engine = ReportGenerationEngine()
