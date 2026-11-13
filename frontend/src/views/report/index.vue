<template>
  <section class="page" data-module="report">
    <header class="page-head">
      <div>
        <h2>采集统计报表管理</h2>
        <p class="page-desc">按统计周期与报表编号分段生成：生成段、当前批次、诊断码与快照全部持久化，中断后从失败段恢复，历史版本完成前保持可查看。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记统计报表</button>
        <button class="btn" type="button" @click="exportRows">导出采集统计报表清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>当前版本/批次</th>
          <th>生成进度</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span>V{{ row.current_version ?? 0 }}</span>
            <span v-if="row.生成批次" class="muted"> · {{ row.生成批次 }} 批次 · 重试 {{ row.累计重试 ?? 0 }}</span>
          </td>
          <td>
            <span :class="['status-tag', statusClass(row.status)]">{{ row.status }}</span>
            <button class="link" type="button" @click="openJobs(row)">查看生成段</button>
          </td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openVersions(row)">历史版本</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无采集统计报表数据，可先登记统计报表</td>
        </tr>
      </tbody>
    </table>

    <!-- 生成段抽屉：刷新页面后仍从持久化数据还原上次位置 -->
    <div v-if="jobDrawer.open" class="drawer-mask" @click.self="jobDrawer.open = false">
      <div class="drawer">
        <div class="drawer-head">
          <h3>生成任务与生成段（{{ jobDrawer.reportCode }}）</h3>
          <button class="btn ghost" type="button" @click="jobDrawer.open = false">关闭</button>
        </div>
        <p v-if="jobDrawer.job" class="muted">
          任务 {{ jobDrawer.job.job_id }} · 状态
          <span :class="['status-tag', statusClass(jobDrawer.job.state)]">{{ jobDrawer.job.state }}</span>
          · 当前批次 {{ jobDrawer.job.current_batch }}/{{ jobDrawer.job.total_batches }}
          · 快照行数 {{ jobDrawer.job.snapshot_row_count }} · 累计重试 {{ jobDrawer.job.retry_count }}
          <template v-if="jobDrawer.job.diagnostic_code">
            · 诊断码 <strong class="error-text">{{ jobDrawer.job.diagnostic_code }}</strong>
          </template>
        </p>
        <p v-if="jobDrawer.job?.error_message" class="error-text">{{ jobDrawer.job.error_message }}</p>
        <div v-if="jobDrawer.job" class="drawer-actions">
          <button
            class="btn primary"
            type="button"
            :disabled="jobDrawer.job.state === '已生成' || resuming"
            @click="resumeJob(jobDrawer.job.job_id)"
          >
            从失败段恢复
          </button>
          <span class="muted">已完成段不会重新计算，只补未完成区间</span>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>生成段</th><th>统计周期</th><th>报表编号</th><th>批次</th>
              <th>状态</th><th>重试</th><th>诊断码</th><th>段结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="seg in jobDrawer.segments" :key="seg.segment_id">
              <td>{{ seg.segment_id }}</td>
              <td>{{ seg.统计周期 }}</td>
              <td>{{ seg.报表编号 }}</td>
              <td>{{ seg.batch_index }}</td>
              <td><span :class="['status-tag', statusClass(seg.state)]">{{ seg.state }}</span></td>
              <td>{{ seg.retries }}</td>
              <td>{{ seg.diagnostic_code || '—' }}</td>
              <td class="muted">{{ seg.result ? `采集量 ${seg.result.采集量合计} / 异常 ${seg.result.异常量合计}` : '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 历史版本抽屉：完成前的历史已生成版本保持可查看 -->
    <div v-if="versionDrawer.open" class="drawer-mask" @click.self="versionDrawer.open = false">
      <div class="drawer">
        <div class="drawer-head">
          <h3>历史版本（{{ versionDrawer.reportCode }}）</h3>
          <button class="btn ghost" type="button" @click="versionDrawer.open = false">关闭</button>
        </div>
        <table class="data-table">
          <thead>
            <tr><th>版本</th><th>生成时间</th><th>生成段</th><th>批次</th><th>重试</th><th>快照行数</th><th>摘要</th><th>归档</th></tr>
          </thead>
          <tbody>
            <tr v-for="ver in versionDrawer.versions" :key="ver.version_no">
              <td>V{{ ver.version_no }}</td>
              <td>{{ ver.generated_at }}</td>
              <td>{{ ver.segment_count }}</td>
              <td>{{ ver.segments[0] ? Math.max(...ver.segments.map((s: any) => s.批次)) : '—' }}</td>
              <td>{{ ver.retry_count }}</td>
              <td>{{ ver.snapshot_row_count }}</td>
              <td class="muted">
                采集量 {{ ver.summary.采集量合计 }} / 异常 {{ ver.summary.异常量合计 }}
                / 来源 {{ (ver.summary.来源计划 || []).join('、') }}
              </td>
              <td>{{ ver.archived ? '已归档' : '否' }}</td>
            </tr>
            <tr v-if="!versionDrawer.versions.length">
              <td colspan="8" class="empty-state">暂无历史版本，生成完成后在此查看</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集统计报表记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/report'
const columns = ["报表编号", "统计维度", "统计周期", "指标集合", "数据来源", "生成时间", "报表格式", "分发方式"]
const statuses = ["草稿", "生成中", "已中断", "生成失败", "已生成", "已归档"]
const stats = ref([
  { label: "统计报表总数", value: 0 },
  { label: "已生成报表", value: 0 },
  { label: "已归档报表", value: 0 },
  { label: "中断/失败", value: 0 },
])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = reactive<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const resuming = ref(false)
let timer: number | undefined

const jobDrawer = reactive<{ open: boolean; reportCode: string; job: Row | null; segments: Row[] }>({
  open: false,
  reportCode: '',
  job: null,
  segments: [],
})
const versionDrawer = reactive<{ open: boolean; reportCode: string; versions: Row[] }>({
  open: false,
  reportCode: '',
  versions: [],
})

function resetFilters() {
  Object.keys(filters).forEach((key) => delete filters[key])
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '统计报表登记入口尚未接入审批流'
}

function availableActions(row: Row): string[] {
  if (row.status === '已归档') return []
  if (row.status === '已生成') return ['生成报表', '归档报表']
  return ['生成报表']
}

function statusClass(status: string): string {
  if (status === '已生成' || status === '已完成') return 'tag-ok'
  if (status === '已中断' || status === '生成失败' || status === '失败') return 'tag-bad'
  if (status === '生成中' || status === '计算中') return 'tag-run'
  return 'tag-idle'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '采集统计报表动作未生效，请稍后重试')
    }
    await reload()
    if (action === '生成报表') await openJobs(row)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集统计报表操作失败'
  }
}

async function openJobs(row: Row) {
  jobDrawer.open = true
  jobDrawer.reportCode = String(row.报表编号)
  jobDrawer.job = null
  jobDrawer.segments = []
  try {
    const response = await request(`${ENDPOINT}/jobs?report_id=${row.id}`)
    const payload = await response.json()
    const latest = (payload.items ?? [])[0]
    if (!latest) return
    const detailResp = await request(`${ENDPOINT}/jobs/${latest.job_id}`)
    const detail = await detailResp.json()
    jobDrawer.job = detail
    jobDrawer.segments = detail.segments ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '生成任务读取失败'
  }
}

async function resumeJob(jobId: string) {
  resuming.value = true
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/jobs/${jobId}/resume`, { method: 'POST' })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message)
    if (jobDrawer.job) {
      const detailResp = await request(`${ENDPOINT}/jobs/${jobId}`)
      const detail = await detailResp.json()
      jobDrawer.job = detail
      jobDrawer.segments = detail.segments ?? []
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复生成失败'
  } finally {
    resuming.value = false
  }
}

function openVersions(row: Row) {
  versionDrawer.open = true
  versionDrawer.reportCode = String(row.报表编号)
  versionDrawer.versions = Array.isArray(row.versions) ? row.versions : []
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) throw new Error('统计报表列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '统计报表总数', value: total.value },
      { label: '已生成报表', value: rows.value.filter((r) => r.status === '已生成').length },
      { label: '已归档报表', value: rows.value.filter((r) => r.status === '已归档').length },
      { label: '中断/失败', value: rows.value.filter((r) => ['已中断', '生成失败'].includes(r.status)).length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集统计报表列表读取失败'
  }
}

// 生成中/中断的任务抽屉打开时轮询，保证返回或刷新页面后进度对得上
onMounted(() => {
  void reload()
  timer = window.setInterval(() => {
    if (jobDrawer.open && jobDrawer.job && ['生成中', '已中断', '生成失败'].includes(jobDrawer.job.state)) {
      void openJobs({ id: jobDrawer.job.report_id, 报表编号: jobDrawer.reportCode })
    }
  }, 3000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<style scoped>
.status-tag { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; white-space: nowrap; }
.tag-ok { background: #e7f6ec; color: #177245; }
.tag-bad { background: #fde8e7; color: #b42318; }
.tag-run { background: #e8f0fe; color: #1f6feb; }
.tag-idle { background: #f2f4f7; color: #475467; }
.muted { color: #667085; font-size: 12px; }
.drawer-mask { position: fixed; inset: 0; background: rgba(16, 24, 40, 0.45); z-index: 20; display: flex; justify-content: flex-end; }
.drawer { width: 72%; max-width: 980px; background: #fff; height: 100%; overflow: auto; padding: 18px 20px; }
.drawer-head { display: flex; justify-content: space-between; align-items: center; }
.drawer-actions { display: flex; gap: 10px; align-items: center; margin: 8px 0; }
</style>
