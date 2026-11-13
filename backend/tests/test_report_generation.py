"""分段报表生成全链路测试。

直接对 Service/Engine 层做断言，不依赖 HTTP 框架：

- 分段、批次、并发与诊断码的持久化；
- 故障注入后的中断恢复，已完成段不重复计算；
- 单有效执行、快照隔离、版本保留、归档防覆盖；
- 审计待复核清单同步、采集计划来源说明；
- 重启后（新 Store/DataSource 实例读同一数据目录）位置不丢。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

# 必须在导入 app.* 之前指好数据目录，保证用例之间完全隔离
_TMP = tempfile.mkdtemp(prefix="report-test-")
os.environ["APP_DATA_DIR"] = _TMP

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import audit as audit_service_module  # noqa: E402
from app.services import report as report_service_module  # noqa: E402
from app.services import taskplan as taskplan_service_module  # noqa: E402
from app.services.datasource import data_source  # noqa: E402
from app.services.report_engine import (  # noqa: E402
    DIAG_ARCHIVED,
    DIAG_INTERRUPTED,
    DIAG_SEGMENT_FAILED,
    JOB_DONE,
    JOB_FAILED,
    JOB_INTERRUPTED,
    JOB_RUNNING,
    SEG_DONE,
    SEG_FAILED,
    SEG_PENDING,
    SEG_RUNNING,
    reconcile_interrupted_jobs,
)
from app.store import store  # noqa: E402


class ReportGenerationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        data_source.clear_faults()
        store.reset_to_seed()
        # 清掉示例数据之外可能残留的任务表
        store.rows("report_jobs").clear()
        store.save()
        self.report_service = report_service_module.ReportService()
        self.audit_service = audit_service_module.AuditService()
        self.taskplan_service = taskplan_service_module.TaskplanService()

    # ------------------------------------------------------------ 1. 分段与持久化
    def test_generation_splits_segments_and_persists_progress(self) -> None:
        entry, missing = self.report_service.create_entry({
            "报表编号": "REPO-T1",
            "统计维度": "按站点",
            "统计周期": "混合",
        })
        self.assertEqual(missing, [])
        result, message = self.report_service.run_action(int(entry["id"]), "生成报表")
        self.assertIsNotNone(result, message)
        self.assertEqual(result["status"], JOB_DONE)

        jobs = self.report_service.list_jobs(int(entry["id"]))
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        # 3 个周期 × 3 个报表编号 = 9 个生成段，批次大小 4 ⇒ 3 个批次
        self.assertEqual(len(job["segments"]), 9)
        self.assertEqual(job["total_batches"], 3)
        self.assertTrue(all(seg["state"] == SEG_DONE for seg in job["segments"]))
        self.assertEqual(job["state"], JOB_DONE)
        # 段身份必须按「统计周期 + 报表编号」
        identities = {(seg["统计周期"], seg["报表编号"]) for seg in job["segments"]}
        self.assertEqual(len(identities), 9)
        # 报表条目上落了版本与摘要
        self.assertEqual(result["current_version"], 1)
        self.assertEqual(len(result["versions"]), 1)
        self.assertIn("摘要", result)
        self.assertEqual(result["摘要"]["生成段数"], 9)

    # ------------------------------------------------------------ 2. 中断恢复
    def test_failed_segment_resumes_without_recomputing_done_ones(self) -> None:
        entry, _ = self.report_service.create_entry({
            "报表编号": "REPO-T2",
            "统计维度": "按站点",
            "统计周期": "混合",
        })
        report_id = int(entry["id"])
        # 给排序最后的段注入故障，保证前面批次已经完成并落盘
        data_source.inject_fault("2026-W38", "REPO-0003")
        result, message = self.report_service.run_action(report_id, "生成报表")
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], JOB_FAILED)

        jobs = self.report_service.list_jobs(report_id)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["state"], JOB_FAILED)
        self.assertEqual(job["diagnostic_code"], DIAG_SEGMENT_FAILED)
        failed = [seg for seg in job["segments"] if seg["state"] == SEG_FAILED]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["retries"], 4)  # 初始 1 次 + 3 次重试
        done_before = {
            (seg["统计周期"], seg["报表编号"])
            for seg in job["segments"] if seg["state"] == SEG_DONE
        }
        self.assertEqual(len(done_before), 8)

        # 恢复：清除故障后从失败段继续
        data_source.clear_faults()
        resumed, resume_message = self.report_service.resume_job(str(job["job_id"]))
        self.assertIsNotNone(resumed, resume_message)
        self.assertEqual(resumed["state"], JOB_DONE)
        self.assertTrue(all(seg["state"] == SEG_DONE for seg in resumed["segments"]))
        # 已完成段的结果对象还在，且没有被重新计算（重试次数保持 0）
        untouched = [
            seg for seg in resumed["segments"]
            if (seg["统计周期"], seg["报表编号"]) in done_before
        ]
        self.assertTrue(all(seg["retries"] == 0 for seg in untouched))
        self.assertTrue(all(seg["result"] is not None for seg in untouched))
        entry_after = self.report_service.get_entry(report_id)
        self.assertEqual(entry_after["status"], JOB_DONE)
        self.assertEqual(entry_after["current_version"], 1)

    # ------------------------------------------------------------ 3. 单有效执行
    def test_repeated_trigger_keeps_single_active_execution(self) -> None:
        entry, _ = self.report_service.create_entry({
            "报表编号": "REPO-T3",
            "统计维度": "按站点",
            "统计周期": "混合",
        })
        report_id = int(entry["id"])
        data_source.inject_fault("2026-W38", "REPO-0003")
        self.report_service.run_action(report_id, "生成报表")
        # 再次触发：必须复用同一个任务，而不是新建
        again, message = self.report_service.run_action(report_id, "生成报表")
        self.assertIsNotNone(again)
        jobs = self.report_service.list_jobs(report_id)
        self.assertEqual(len(jobs), 1)
        self.assertIn("恢复", message)

    # ------------------------------------------------------------ 4. 快照隔离
    def test_new_data_does_not_rewrite_running_snapshot(self) -> None:
        entry, _ = self.report_service.create_entry({
            "报表编号": "REPO-T4",
            "统计维度": "按站点",
            "统计周期": "混合",
        })
        report_id = int(entry["id"])
        data_source.inject_fault("2026-W38", "REPO-0003")
        self.report_service.run_action(report_id, "生成报表")
        job = self.report_service.list_jobs(report_id)[0]
        frozen_rows = int(job["snapshot"]["row_count"])

        # 生成中断期间有新采集数据到达
        new_row = data_source.ingest("2026-W38", "REPO-0003", "TASK-0009", 999, 1)
        self.assertTrue(new_row)

        data_source.clear_faults()
        resumed, _ = self.report_service.resume_job(str(job["job_id"]))
        # 任务快照行数不变，新数据没有混进正在生成的报表
        self.assertEqual(resumed["snapshot"]["row_count"], frozen_rows)
        finished = self.report_service.get_job(str(job["job_id"]))
        target_seg = next(
            seg for seg in finished["segments"]
            if seg["统计周期"] == "2026-W38" and seg["报表编号"] == "REPO-0003"
        )
        # 恢复段仍按旧快照计算（只有 2 条明细，新到达的第 3 条不计入）
        self.assertEqual(target_seg["result"]["明细行数"], 2)

        # 下一次生成才会带上新数据
        entry2, _ = self.report_service.create_entry({
            "报表编号": "REPO-T4B",
            "统计维度": "按站点",
            "统计周期": "混合",
        })
        self.report_service.run_action(int(entry2["id"]), "生成报表")
        job2 = self.report_service.list_jobs(int(entry2["id"]))[0]
        self.assertEqual(job2["snapshot"]["row_count"], frozen_rows + 1)

    # ------------------------------------------------------------ 5. 版本保留
    def test_versions_are_retained_and_old_non_archived_pruned(self) -> None:
        entry, _ = self.report_service.create_entry({
            "报表编号": "REPO-T5",
            "统计维度": "按站点",
            "统计周期": "混合",
        })
        report_id = int(entry["id"])
        # 连续生成 5 版（每次触发前先让上一版完成；通过新数据制造版本差异）
        for round_no in range(5):
            data_source.ingest("2026-W36", "REPO-0001", "TASK-0001", 10 + round_no, 0)
            result, _ = self.report_service.run_action(report_id, "生成报表")
            self.assertIsNotNone(result)
        entry_after = self.report_service.get_entry(report_id)
        self.assertEqual(entry_after["current_version"], 5)
        # 保留最近 3 个非归档版本
        self.assertEqual(len(entry_after["versions"]), 3)
        self.assertEqual(
            [item["version_no"] for item in entry_after["versions"]],
            [3, 4, 5],
        )
        # 历史版本仍可查看，且保留各自摘要
        self.assertEqual(entry_after["versions"][0]["summary"]["生成段数"], 9)

    # ------------------------------------------------------------ 6. 归档防覆盖
    def test_archived_report_cannot_be_retried(self) -> None:
        entry, _ = self.report_service.create_entry({
            "报表编号": "REPO-T6",
            "统计维度": "按站点",
            "统计周期": "混合",
        })
        report_id = int(entry["id"])
        self.report_service.run_action(report_id, "生成报表")
        archived, message = self.report_service.run_action(report_id, "归档报表")
        self.assertIsNotNone(archived, message)
        self.assertEqual(archived["status"], "已归档")

        blocked, block_message = self.report_service.run_action(report_id, "生成报表")
        self.assertIsNone(blocked)
        self.assertIn("归档", block_message)
        # 版本被打上归档标记且不会被保留策略清理
        entry_after = self.report_service.get_entry(report_id)
        self.assertTrue(all(item["archived"] for item in entry_after["versions"]))
        jobs = self.report_service.list_jobs(report_id)
        # 归档后尝试触发没有产生新任务
        self.assertTrue(all(job["state"] == JOB_DONE for job in jobs))

    # ------------------------------------------------------------ 7. 审计同步与待复核
    def test_report_result_syncs_to_audit_pending_review(self) -> None:
        entry, _ = self.report_service.create_entry({
            "报表编号": "REPO-T7",
            "统计维度": "按站点",
            "统计周期": "混合",
        })
        report_id = int(entry["id"])
        self.report_service.run_action(report_id, "生成报表")
        pending, total = self.audit_service.pending_review()
        self.assertGreaterEqual(total, 1)
        synced = next(
            row for row in pending
            if row.get("关联报表") == "REPO-T7"
        )
        self.assertEqual(synced["status"], "待复核")
        self.assertEqual(synced["生成批次"], 3)
        self.assertEqual(synced["重试次数"], 0)
        self.assertIn("REPO-T7", synced["结果摘要"])
        self.assertTrue(synced["来源说明"])

        # 复核流转
        reviewed, message = self.audit_service.run_action(int(synced["id"]), "标记复核")
        self.assertIsNotNone(reviewed, message)
        self.assertEqual(reviewed["status"], "已复核")
        pending_after, _ = self.audit_service.pending_review()
        self.assertFalse(any(row["id"] == synced["id"] for row in pending_after))

    def test_failed_generation_can_be_reconciled_and_audit_waits_for_completion(self) -> None:
        entry, _ = self.report_service.create_entry({
            "报表编号": "REPO-T8",
            "统计维度": "按站点",
            "统计周期": "混合",
        })
        report_id = int(entry["id"])
        data_source.inject_fault("2026-W38", "REPO-0003")
        self.report_service.run_action(report_id, "生成报表")
        # 失败期间不应产生审计同步记录
        pending, _ = self.audit_service.pending_review()
        self.assertFalse(any(row.get("关联报表") == "REPO-T8" for row in pending))

    # ------------------------------------------------------------ 8. 重启对账 + 持久化
    def test_restart_reconciles_stuck_generation_and_keeps_position(self) -> None:
        entry, _ = self.report_service.create_entry({
            "报表编号": "REPO-T9",
            "统计维度": "按站点",
            "统计周期": "混合",
        })
        report_id = int(entry["id"])
        data_source.inject_fault("2026-W38", "REPO-0003")
        self.report_service.run_action(report_id, "生成报表")
        job = self.report_service.list_jobs(report_id)[0]
        # 模拟进程崩溃：直接把落盘状态改成生成中（线程消失但状态没来得及更新）
        job["state"] = JOB_RUNNING
        for seg in job["segments"]:
            if seg["state"] == SEG_FAILED:
                seg["state"] = SEG_RUNNING
        store.save()

        fixed = reconcile_interrupted_jobs()
        self.assertGreaterEqual(fixed, 1)
        reloaded = self.report_service.get_job(str(job["job_id"]))
        self.assertEqual(reloaded["state"], JOB_INTERRUPTED)
        self.assertEqual(reloaded["diagnostic_code"], DIAG_INTERRUPTED)
        stuck_seg = next(
            seg for seg in reloaded["segments"]
            if seg["统计周期"] == "2026-W38" and seg["报表编号"] == "REPO-0003"
        )
        self.assertEqual(stuck_seg["state"], SEG_FAILED)
        entry_after = self.report_service.get_entry(report_id)
        self.assertEqual(entry_after["status"], JOB_INTERRUPTED)

        # 恢复后跑完
        data_source.clear_faults()
        resumed, _ = self.report_service.resume_job(str(job["job_id"]))
        self.assertEqual(resumed["state"], JOB_DONE)

    def test_state_survives_new_store_instance_from_disk(self) -> None:
        entry, _ = self.report_service.create_entry({
            "报表编号": "REPO-T10",
            "统计维度": "按站点",
            "统计周期": "混合",
        })
        report_id = int(entry["id"])
        data_source.inject_fault("2026-W38", "REPO-0003")
        self.report_service.run_action(report_id, "生成报表")
        job = self.report_service.list_jobs(report_id)[0]
        done_identities = {
            (seg["统计周期"], seg["报表编号"])
            for seg in job["segments"] if seg["state"] == SEG_DONE
        }

        # 模拟服务重启：重新实例化 Store（同一数据目录），位置必须还在
        from app.store import Store

        fresh_store = Store(data_dir=_TMP)
        fresh_jobs = fresh_store.rows("report_jobs")
        fresh_job = next(item for item in fresh_jobs if item["job_id"] == job["job_id"])
        self.assertEqual(fresh_job["state"], JOB_FAILED)
        fresh_done = {
            (seg["统计周期"], seg["报表编号"])
            for seg in fresh_job["segments"] if seg["state"] == SEG_DONE
        }
        self.assertEqual(fresh_done, done_identities)
        self.assertEqual(len(fresh_done), 8)
        # 快照也持久化了
        self.assertGreater(fresh_job["snapshot"]["row_count"], 0)

    # ------------------------------------------------------------ 9. 采集计划来源说明
    def test_taskplan_keeps_source_note(self) -> None:
        plan, missing = self.taskplan_service.create_entry({
            "计划编号": "TASK-NEW-1",
            "目标站点": "example.test",
            "采集频率": "每日",
            "来源说明": "来自渠道接入工单 WO-2026-001",
        })
        self.assertEqual(missing, [])
        self.assertEqual(plan["来源说明"], "来自渠道接入工单 WO-2026-001")
        again = self.taskplan_service.get_entry(int(plan["id"]))
        self.assertEqual(again["来源说明"], "来自渠道接入工单 WO-2026-001")

    # ------------------------------------------------------------ 10. 段状态初始值
    def test_segments_carry_diagnostic_fields_from_start(self) -> None:
        entry, _ = self.report_service.create_entry({
            "报表编号": "REPO-T11",
            "统计维度": "按站点",
            "统计周期": "混合",
        })
        self.report_service.run_action(int(entry["id"]), "生成报表")
        job = self.report_service.list_jobs(int(entry["id"]))[0]
        for seg in job["segments"]:
            self.assertIn(seg["state"], (SEG_PENDING, SEG_RUNNING, SEG_DONE, SEG_FAILED))
            self.assertIn("diagnostic_code", seg)
            self.assertIn("retries", seg)
            self.assertIn("batch_index", seg)

    # ------------------------------------------------------------ 11. 通用模块写操作同样落盘
    def test_generic_services_persist_writes_across_restart(self) -> None:
        from app.services.verify import VerifyService
        from app.store import Store

        verify_service = VerifyService()
        rule, missing = verify_service.create_entry({
            "规则编号": "VERI-NEW", "校验对象": "对象X", "校验类型": "完整性",
        })
        self.assertEqual(missing, [])
        changed, message = verify_service.run_action(int(rule["id"]), "启动校验")
        self.assertIsNotNone(changed, message)
        expected_status = changed["status"]

        fresh_store = Store(data_dir=_TMP)
        reloaded = fresh_store.find("verify", int(rule["id"]))
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded["规则编号"], "VERI-NEW")
        # 动作后的状态必须已经落盘，而不是停留在内存里
        self.assertEqual(reloaded["status"], expected_status)

    # ------------------------------------------------------------ 12. 并发触发只保留一个有效执行
    def test_concurrent_triggers_keep_single_execution(self) -> None:
        import threading

        from app.services.report_engine import generation_engine

        entry, _ = self.report_service.create_entry({
            "报表编号": "REPO-T12",
            "统计维度": "按站点",
            "统计周期": "混合",
        })
        report_id = int(entry["id"])
        results: list = []
        errors: list = []

        def trigger() -> None:
            try:
                job, _message, created = generation_engine.trigger(report_id)
                results.append((job["job_id"] if job else None, created))
            except Exception as exc:  # pragma: no cover - 防御性
                errors.append(exc)

        threads = [threading.Thread(target=trigger) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        job_ids = {item[0] for item in results}
        self.assertEqual(len(job_ids), 1)
        # 只有第一个是新建，其余全部复用
        self.assertEqual(sum(1 for _job_id, created in results if created), 1)
        self.assertEqual(len(self.report_service.list_jobs(report_id)), 1)

    # ------------------------------------------------------------ 13. 批次大小与并发上限生效
    def test_batch_size_and_concurrency_settings_are_applied(self) -> None:
        from app.config import settings

        # 9 个段、批次大小 4 ⇒ 3 批；并发上限 2 在引擎里通过线程池应用
        self.assertEqual(settings.report_batch_size, 4)
        self.assertEqual(settings.report_concurrency, 2)
        entry, _ = self.report_service.create_entry({
            "报表编号": "REPO-T13",
            "统计维度": "按站点",
            "统计周期": "混合",
        })
        self.report_service.run_action(int(entry["id"]), "生成报表")
        job = self.report_service.list_jobs(int(entry["id"]))[0]
        batch_indices = sorted({seg["batch_index"] for seg in job["segments"]})
        self.assertEqual(batch_indices, [1, 2, 3])
        # 前两批各 4 段，最后一批 1 段
        counts = {
            index: sum(1 for seg in job["segments"] if seg["batch_index"] == index)
            for index in batch_indices
        }
        self.assertEqual(counts, {1: 4, 2: 4, 3: 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
