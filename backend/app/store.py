"""持久化数据仓库：每个业务模块一张表，全部落盘到本地 JSON。

之前的实现只把数据放在进程内存里，服务重启或刷新后状态全丢，正在生成的
报表会永远停在「生成中」。这里改成文件仓库：

- 首次启动没有数据文件时，用 ``app.seed`` 的示例数据初始化并立即落盘；
- 所有写操作都经过同一把可重入锁，并以「临时文件 + 原子替换」方式写盘，
  写到一半进程退出也不会留下半截文件；
- 读取直接返回内存对象，但写路径必须走本模块提供的方法，保证每次变更
  都被持久化，重新进入、返回或刷新页面时结果对得上。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，克隆下来就能起。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

from app.config import settings
from app.seed import SEED_ROWS

_STATE_FILENAME = "state.json"


class Store:
    def __init__(self, data_dir: str | None = None) -> None:
        self._lock = threading.RLock()
        self._data_dir = data_dir or settings.data_dir
        self._state_path = os.path.join(self._data_dir, _STATE_FILENAME)
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._load()

    # ------------------------------------------------------------------ 持久化
    def _load(self) -> None:
        if os.path.exists(self._state_path):
            try:
                with open(self._state_path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                tables = payload.get("tables")
                if isinstance(tables, dict):
                    self._tables = {
                        name: [dict(row) for row in rows]
                        for name, rows in tables.items()
                        if isinstance(rows, list)
                    }
                    return
            except (json.JSONDecodeError, OSError):
                # 文件损坏时不静默使用脏数据，退回到示例数据重新初始化
                pass
        self._tables = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._flush()

    def _flush(self) -> None:
        """把当前全量状态原子写入磁盘；调用方需要持有锁。"""
        os.makedirs(self._data_dir, exist_ok=True)
        payload = {"version": 1, "tables": self._tables}
        fd, tmp_path = tempfile.mkstemp(
            prefix=".state-", suffix=".tmp", dir=self._data_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._state_path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def save(self) -> None:
        """对外暴露的持久化入口：业务服务改完对象后调用，确保落盘。"""
        with self._lock:
            self._flush()

    def reset_to_seed(self) -> None:
        """测试或运维排障时使用：清空全部表并恢复示例数据。"""
        with self._lock:
            self._tables = {
                name: [dict(row) for row in rows]
                for name, rows in SEED_ROWS.items()
            }
            self._flush()

    # ------------------------------------------------------------------ 表访问
    def module_names(self) -> list[str]:
        with self._lock:
            return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in self.rows(module):
                if int(row.get("id", 0)) == entry_id:
                    return row
        return None

    def next_id(self, module: str) -> int:
        with self._lock:
            return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def insert(self, module: str, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if "id" not in entry:
                entry["id"] = self.next_id(module)
            self.rows(module).append(entry)
            self._flush()
            return entry

    def overview(self) -> dict[str, object]:
        with self._lock:
            modules: list[dict[str, object]] = []
            for name in self.module_names():
                rows = self.rows(name)
                modules.append({
                    "name": name,
                    "created": len(rows),
                    "pending": sum(1 for row in rows if row.get("pending")),
                    "abnormal": sum(1 for row in rows if row.get("abnormal")),
                })
            cards = [
                {"label": "业务模块", "value": len(modules)},
                {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
                {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
                {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
            ]
            return {"cards": cards, "modules": modules}


store = Store()
