<template>
  <section class="page" data-module="audit">
    <header class="page-head">
      <div>
        <h2>采集审计日志管理</h2>
        <p class="page-desc">维护采集审计的操作类型、执行记录与复核状态；报表生成结果自动同步到待复核清单，携带生成批次、重试次数与诊断码，可追溯到采集计划。</p>
      </div>
      <div class="page-actions">
        <button class="btn" :class="{ primary: tab === 'ledger' }" type="button" @click="switchTab('ledger')">审计台账</button>
        <button class="btn" :class="{ primary: tab === 'pending' }" type="button" @click="switchTab('pending')">
          待复核清单{{ pendingTotal ? `（${pendingTotal}）` : '' }}
        </button>
        <button class="btn" type="button" @click="exportRows">导出采集审计日志清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <!-- 待复核清单：报表生成结果同步入口 -->
    <template v-if="tab === 'pending'">
      <form class="filter-bar" @submit.prevent="loadPending">
        <label class="filter-item">
          <span>按记录编号/关联报表检索</span>
          <input v-model="pendingKeyword" placeholder="如 REPO-0001" />
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="pendingKeyword = ''; loadPending()">重置条件</button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th>记录编号</th><th>关联报表</th><th>结果摘要</th><th>生成批次</th>
            <th>重试次数</th><th>诊断码</th><th>来源说明</th><th>执行时间</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in pendingRows" :key="String(row.id)">
            <td>{{ row.记录编号 }}</td>
            <td>{{ row.关联报表 ?? '—' }}</td>
            <td>{{ row.结果摘要 }}</td>
            <td>{{ row.生成批次 ?? '—' }}</td>
            <td>{{ row.重试次数 ?? 0 }}</td>
            <td>{{ row.诊断码 || '—' }}</td>
            <td class="muted">{{ row.来源说明 ?? '—' }}</td>
            <td>{{ row.执行时间 }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="runAction('标记复核', row, true)">标记复核</button>
            </td>
          </tr>
          <tr v-if="!pendingRows.length">
            <td colspan="9" class="empty-state">暂无待复核记录，报表生成完成后结果会自动同步到这里</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot"><span>共 {{ pendingTotal }} 条待复核记录</span><span v-if="errorMessage" class="error-text">{{ errorMessage }}</span></footer>
    </template>

    <!-- 全量台账 -->
    <template v-else>
      <form class="filter-bar" @submit.prevent="reload">
        <label v-for="field in filterFields" :key="field" class="filter-item">
          <span>{{ field }}</span>
          <input v-model="filters[field]" :placeholder="`按${field}检索`" />
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in columns" :key="column">{{ column }}</th>
            <th>生成批次</th><th>重试次数</th><th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="String(row.id)">
            <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
            <td>{{ row.生成批次 ?? '—' }}</td>
            <td>{{ row.重试次数 ?? 0 }}</td>
            <td class="row-actions">
              <button
                v-for="action in actions"
                :key="action"
                class="link"
                type="button"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length + 3" class="empty-state">暂无采集审计日志数据</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot">
        <span>共 {{ total }} 条采集审计日志记录</span>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/audit'
const columns = ["记录编号", "操作类型", "采集计划", "执行人", "执行时间", "结果摘要", "变更内容", "复核状态"]
const actions = ["标记复核", "归档记录"]
const stats = ref([
  { label: "审计记录总数", value: 0 },
  { label: "待复核记录", value: 0 },
  { label: "已复核记录", value: 0 },
  { label: "已归档记录", value: 0 },
])

const tab = ref<'ledger' | 'pending'>('pending')
const rows = ref<Row[]>([])
const total = ref(0)
const pendingRows = ref<Row[]>([])
const pendingTotal = ref(0)
const pendingKeyword = ref('')
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function switchTab(next: 'ledger' | 'pending') {
  tab.value = next
  errorMessage.value = ''
  if (next === 'pending') void loadPending()
  else void reload()
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function runAction(action: string, row: Row, fromPending = false) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '采集审计日志动作未生效，请稍后重试')
    }
    if (fromPending) await loadPending()
    else await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集审计日志操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) throw new Error('审计记录列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '审计记录总数', value: total.value },
      { label: '待复核记录', value: rows.value.filter((r) => r.status === '待复核').length },
      { label: '已复核记录', value: rows.value.filter((r) => r.status === '已复核').length },
      { label: '已归档记录', value: rows.value.filter((r) => r.status === '已归档').length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集审计日志列表读取失败'
  }
}

async function loadPending() {
  errorMessage.value = ''
  const query = new URLSearchParams(pendingKeyword.value ? { keyword: pendingKeyword.value } : {}).toString()
  try {
    const response = await request(`${ENDPOINT}/pending-review?${query}`)
    if (!response.ok) throw new Error('待复核清单读取失败')
    const payload = await response.json()
    pendingRows.value = payload.items ?? []
    pendingTotal.value = payload.total ?? pendingRows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '待复核清单读取失败'
  }
}

onMounted(loadPending)
</script>

<style scoped>
.muted { color: #667085; font-size: 12px; }
</style>
