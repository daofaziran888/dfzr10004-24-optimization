"""运行配置：端口、跨域、运行环境与分段生成策略。

分段生成相关参数集中在这里，方便按部署环境调整；也允许通过环境变量覆盖，
测试时把数据目录指到临时目录即可做到用例之间完全隔离。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_name: str = "智能采集质量管控平台"
    env: str = "local"
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200

    # 持久化数据目录：报表版本、生成段、快照和审计清单都落在这下面
    data_dir: str = field(default_factory=lambda: os.environ.get("APP_DATA_DIR", "data"))

    # 分段生成策略（题目允许由实现决定，这里给出固定默认值并支持环境变量覆盖）
    # 一个批次最多包含的生成段数量
    report_batch_size: int = field(default_factory=lambda: _int_env("REPORT_BATCH_SIZE", 4))
    # 同一报表任务内部，批次执行的并发上限
    report_concurrency: int = field(default_factory=lambda: _int_env("REPORT_CONCURRENCY", 2))
    # 单段在一次生成执行中的最大重试次数
    segment_max_retries: int = field(default_factory=lambda: _int_env("SEGMENT_MAX_RETRIES", 3))
    # 每个报表保留的最近已生成版本数量（超出后清理旧版本，归档版本不清理）
    report_version_keep: int = field(default_factory=lambda: _int_env("REPORT_VERSION_KEEP", 3))


settings = Settings()
