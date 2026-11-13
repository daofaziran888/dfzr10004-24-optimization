# 智能采集质量管控平台

面向数据采集团队的质量管控与效率优化平台。覆盖采集任务编排、抓取结果校验、采集链路监控、数据格式转换、去重比对、异常重试、采集审计与合规管理的完整采集质控链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集任务编排 | `taskplan` | 采集计划 | 计划编号、目标站点、采集频率 |
| 采集结果校验 | `verify` | 校验规则 | 规则编号、校验对象、校验类型 |
| 采集链路监控 | `pipeline` | 链路节点 | 节点编号、节点名称、上游节点 |
| 采集格式转换 | `transform` | 转换模板 | 模板编号、输入格式、输出格式 |
| 采集去重比对 | `dedup` | 去重策略 | 策略编号、业务键列、排序字段 |
| 异常重试管理 | `retry` | 重试规则 | 规则编号、异常类型、重试上限 |
| 采集审计日志 | `audit` | 审计记录 | 记录编号、操作类型、采集计划 |
| 采集合规配置 | `compliance` | 合规规则 | 规则编号、站点域名、协议标识 |
| 代理池管理 | `proxy` | 代理节点 | 节点编号、代理地址、代理类型 |
| 采集存储策略 | `storage` | 存储配置 | 配置编号、存储路径、文件格式 |
| 采集告警中心 | `alert` | 告警规则 | 规则编号、监控指标、告警级别 |
| 采集统计报表 | `report` | 统计报表 | 报表编号、统计维度、统计周期 |
| 采集频率控制 | `throttle` | 频率策略 | 策略编号、目标站点、请求间隔 |
| 采集数据标注 | `tag` | 标注记录 | 标注编号、数据记录、标注类型 |
| 采集配置版本 | `version` | 配置版本 | 版本编号、配置名称、版本号 |
| 采集沙箱测试 | `sandbox` | 沙箱实例 | 实例编号、测试脚本、目标站点 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
- 业务数据持久化在 `backend/data/`（`state.json` 与 `datasource.json`，原子写入），
  可用环境变量 `APP_DATA_DIR` 改目录；该目录已在 `.gitignore` 中忽略。

## 报表分段生成与断点恢复

采集统计报表数据量增大后，生成不再整批重算，而是按「统计周期 + 报表编号」拆成
生成段、按批次执行：

- 生成段是计算/重试/恢复的最小单位；每 `REPORT_BATCH_SIZE`（默认 4）个段为一个
  批次，批次内并发上限 `REPORT_CONCURRENCY`（默认 2），单段最多重试
  `SEGMENT_MAX_RETRIES`（默认 3）次；每个报表保留最近 `REPORT_VERSION_KEEP`
  （默认 3）个非归档版本，归档版本永久保留。均可用环境变量覆盖。
- 生成段状态、当前批次、重试次数、诊断码（`E_INTERRUPTED` / `E_SEGMENT_FAILED`
  / `E_ARCHIVED` 等）与开始时的快照全部落盘；进程重启时 `main.py` 的 lifespan
  会把停在「生成中」的任务对账为「已中断」，重新打开页面看到的是可恢复的真实状态。
- 中断后调用 `POST /api/report/jobs/{job_id}/resume` 从失败段恢复，已完成段直接
  跳过，不重复计算；同一报表同时只保留一个有效执行，重复触发复用既有任务。
- 任务开始即冻结采集明细快照，之后新到达的数据不会改写正在生成的报表，冲突时以
  开始时的快照为准；归档后的报表拒绝任何重试覆盖。
- 生成完成后结果自动同步到采集审计台账（`GET /api/audit/pending-review`
  待复核清单），携带生成批次、重试次数、诊断码与来源说明；采集计划清单保留
  「来源说明」字段用于来源回溯。
- 排障演练可用 `POST /api/report/debug/faults` 注入/清除单段计算故障
  （仅进程内生效、不落盘，重启自动清除）。

### 后端测试

```bash
cd backend
.venv/bin/python -m unittest discover -s tests
```

领域：数据工程
子方向：数据采集、爬虫与RPA
