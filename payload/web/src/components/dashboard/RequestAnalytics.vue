<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import {
  Activity,
  ArrowDown,
  ArrowUp,
  ArrowUpRight,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Download,
  RefreshCw,
  SlidersHorizontal,
  Timer,
  X,
} from 'lucide-vue-next'
import { getAnalytics } from '@/api/analytics.ts'
import type {
  AnalyticsQuery,
  AnalyticsRange,
  AnalyticsResponse,
  AnalyticsRequest,
  RequestStatus,
} from '@/types/analytics.ts'
import { usePolling } from '@/composables/usePolling.ts'
import UsageChart from './UsageChart.vue'
import UsageDistribution from './UsageDistribution.vue'
import { clock, compact, duration, number } from './analyticsFormat.ts'

const range = ref<AnalyticsRange>('7d')
const granularity = ref<'day' | 'hour'>('day')
const model = ref('')
const status = ref<RequestStatus | ''>('')
const page = ref(1)
const pageSize = ref(20)
const data = ref<AnalyticsResponse | null>(null)
const loading = ref(true)
const error = ref('')
const expanded = ref<string | null>(null)
const lastUpdated = ref<Date | null>(null)
let controller: AbortController | null = null
let requestVersion = 0

const query = computed<AnalyticsQuery>(() => ({
  range: range.value,
  granularity: granularity.value,
  model: model.value,
  status: status.value,
  page: page.value,
  page_size: pageSize.value,
  timezone_offset: -new Date().getTimezoneOffset(),
}))

async function load() {
  const version = ++requestVersion
  controller?.abort()
  controller = new AbortController()
  loading.value = true
  try {
    const response = await getAnalytics(query.value, controller.signal)
    if (version !== requestVersion) return
    data.value = response
    error.value = ''
    lastUpdated.value = new Date()
    const maxPage = Math.max(
      1,
      Math.ceil(response.requests.total / pageSize.value),
    )
    if (page.value > maxPage) page.value = maxPage
  } catch (err: unknown) {
    if (version !== requestVersion || controller?.signal.aborted) return
    error.value =
      err instanceof Error ? err.message : '暂时无法读取调用统计，请重试。'
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

watch(
  [range, granularity, model, status, pageSize],
  () => {
    page.value = 1
    expanded.value = null
  },
  { flush: 'sync' },
)
watch(query, () => {
  void load()
})
usePolling(async () => {
  if (!loading.value || !data.value) await load()
}, 10000)
onUnmounted(() => {
  requestVersion++
  controller?.abort()
})

const totals = computed(() => data.value?.totals)
const totalPages = computed(() =>
  Math.max(1, Math.ceil((data.value?.requests.total ?? 0) / pageSize.value)),
)
const filtered = computed(
  () =>
    range.value !== '7d' ||
    granularity.value !== 'day' ||
    model.value ||
    status.value,
)
const rangeNames: Record<AnalyticsRange, string> = {
  today: '今天',
  '7d': '最近 7 天',
  '30d': '最近 30 天',
  month: '本月',
  last_month: '上月',
}
const statuses: Record<RequestStatus, string> = {
  success: '成功',
  error: '失败',
  rate_limited: '限流',
  cancelled: '已中断',
  active: '进行中',
}
const dateTime = (value: string) =>
  new Date(value).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
const shortDate = (value: string) => new Date(value).toLocaleDateString('zh-CN')
const successRate = computed(() => {
  const value = totals.value
  return !value ||
    value.success + value.errors + value.rate_limited + value.cancelled === 0
    ? '—'
    : `${value.success_rate.toFixed(1)}%`
})

function clearFilters() {
  range.value = '7d'
  granularity.value = 'day'
  model.value = ''
  status.value = ''
  page.value = 1
}

function token(
  item: AnalyticsRequest,
  key:
    | 'prompt_tokens'
    | 'completion_tokens'
    | 'reasoning_tokens'
    | 'cached_tokens'
    | 'total_tokens',
) {
  return item.usage_known ? number(item[key]) : '—'
}

function exportPage() {
  if (!data.value) return
  const escape = (value: unknown) => {
    let text = String(value ?? '')
    if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`
    return `"${text.replace(/"/g, '""')}"`
  }
  const rows: unknown[][] = [
    [
      '请求 ID',
      '时间',
      '模型',
      '端点',
      '方式',
      '结果',
      '结果状态码',
      '输入 Token',
      '输出 Token',
      '缓存 Token',
      '思考 Token',
      '总 Token',
      '耗时 ms',
      '首个输出 ms',
      '结束原因',
    ],
  ]
  for (const item of data.value.requests.items)
    rows.push([
      item.id,
      item.started_at,
      item.model,
      item.endpoint,
      item.stream ? '流式' : '非流式',
      statuses[item.status],
      item.status_code,
      item.usage_known ? item.prompt_tokens : '',
      item.usage_known ? item.completion_tokens : '',
      item.usage_known ? item.cached_tokens : '',
      item.usage_known ? item.reasoning_tokens : '',
      item.usage_known ? item.total_tokens : '',
      item.duration_ms,
      item.ttft_ms,
      item.finish_reason,
    ])
  const url = URL.createObjectURL(
    new Blob(
      ['\uFEFF' + rows.map((row) => row.map(escape).join(',')).join('\r\n')],
      { type: 'text/csv;charset=utf-8' },
    ),
  )
  const link = document.createElement('a')
  link.href = url
  link.download = `aistudio-requests-${new Date().toISOString().slice(0, 10)}-page-${page.value}.csv`
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}
</script>

<template>
  <section
    class="request-analytics"
    aria-label="调用分析"
  >
    <div
      v-if="data?.demo"
      class="demo-note"
    >
      <span class="demo-dot" />
      <strong>演示预览</strong>
      <span>以下为独立演示数据，用于查看界面效果。</span>
    </div>

    <header class="analytics-heading">
      <div>
        <div class="section-eyebrow">
          USAGE ANALYTICS
        </div>
        <h2>
          调用概览
          <span class="live-label">
            <i />
            实时更新
          </span>
        </h2>
        <p>请求、用量与响应表现，一目了然。</p>
      </div>
      <button
        class="refresh-button"
        :disabled="loading"
        @click="load"
      >
        <RefreshCw
          :class="{ spinning: loading }"
          :size="14"
        />
        <span>{{ loading ? '刷新中' : '刷新数据' }}</span>
      </button>
    </header>

    <div class="analytics-toolbar">
      <div class="filter-controls">
        <label class="filter-field">
          <SlidersHorizontal :size="13" />
          <span>时间</span>
          <select
            v-model="range"
            aria-label="时间范围"
          >
            <option
              v-for="(label, key) in rangeNames"
              :key="key"
              :value="key"
            >
              {{ label }}
            </option>
          </select>
        </label>
        <label class="filter-field">
          <span>模型</span>
          <select
            v-model="model"
            aria-label="筛选模型"
          >
            <option value="">全部模型</option>
            <option
              v-for="name in data?.filters.models ?? []"
              :key="name"
              :value="name"
            >
              {{ name }}
            </option>
          </select>
        </label>
        <label class="filter-field">
          <span>结果</span>
          <select
            v-model="status"
            aria-label="筛选结果"
          >
            <option value="">全部结果</option>
            <option
              v-for="(label, key) in statuses"
              :key="key"
              :value="key"
            >
              {{ label }}
            </option>
          </select>
        </label>
        <button
          v-if="filtered"
          class="clear-filter"
          @click="clearFilters"
        >
          <X :size="12" />
          重置
        </button>
      </div>
      <label class="grain-field">
        <span>粒度</span>
        <select
          v-model="granularity"
          aria-label="时间粒度"
        >
          <option value="day">按天</option>
          <option value="hour">按小时</option>
        </select>
      </label>
    </div>

    <div
      v-if="error"
      class="analytics-error"
      role="alert"
    >
      <span>{{ error }}{{ data ? ' 当前显示上次成功加载的数据。' : '' }}</span>
      <button @click="load">
        重试
      </button>
    </div>

    <template v-if="data && totals">
      <div class="metrics-grid">
        <article class="metric-card requests-metric">
          <div class="metric-top">
            <span>API 请求次数</span>
            <Activity :size="17" />
          </div>
          <div class="metric-value">
            {{ number(totals.requests) }}
            <small>次</small>
          </div>
          <div class="metric-bottom">
            <span class="success-text">{{ number(totals.success) }} 成功</span>
            <span class="separator">·</span>
            <span>
              {{ number(totals.errors + totals.rate_limited) }} 失败 / 限流
            </span>
          </div>
        </article>
        <article class="metric-card">
          <div class="metric-top">
            <span>Token 总用量</span>
            <ArrowUpRight :size="17" />
          </div>
          <div
            class="metric-value"
            :title="number(totals.total_tokens)"
          >
            {{ compact(totals.total_tokens) }}
            <small>Tokens</small>
          </div>
          <div class="metric-bottom">
            <span>
              <ArrowUp :size="11" />
              {{ compact(totals.prompt_tokens) }} 输入
            </span>
            <span>
              <ArrowDown :size="11" />
              {{ compact(totals.completion_tokens) }} 输出
            </span>
          </div>
        </article>
        <article class="metric-card">
          <div class="metric-top">
            <span>平均请求耗时</span>
            <Clock3 :size="17" />
          </div>
          <div class="metric-value">
            {{ duration(totals.avg_duration_ms).replace(' s', '') }}
            <small v-if="totals.avg_duration_ms != null">秒</small>
          </div>
          <div class="metric-bottom">
            P95 耗时
            <span class="metric-subvalue">
              {{ duration(totals.p95_duration_ms) }}
            </span>
          </div>
        </article>
        <article class="metric-card">
          <div class="metric-top">
            <span>平均首个输出</span>
            <Timer :size="17" />
          </div>
          <div class="metric-value">
            {{ duration(totals.avg_ttft_ms).replace(' s', '') }}
            <small v-if="totals.avg_ttft_ms != null">秒</small>
          </div>
          <div class="metric-bottom">
            成功率
            <span class="success-text metric-subvalue">{{ successRate }}</span>
          </div>
        </article>
      </div>

      <div class="runtime-strip">
        <span title="所有模型当前正在处理的请求，不受筛选条件影响">
          <i
            class="runtime-dot"
            :class="{ busy: totals.active_requests > 0 }"
          />
          全局活动请求
          <strong>{{ totals.active_requests }}</strong>
        </span>
        <span>
          <Check :size="13" />
          已完成
          <strong>{{ number(totals.success) }}</strong>
        </span>
        <span>
          <X :size="13" />
          已中断
          <strong>{{ number(totals.cancelled) }}</strong>
        </span>
        <span class="uptime">
          <Clock3 :size="13" />
          服务运行
          <strong>{{ clock(totals.uptime_seconds) }}</strong>
        </span>
      </div>

      <div class="charts-grid">
        <article class="chart-card">
          <div class="chart-card-heading">
            <div>
              <h3>请求趋势</h3>
              <p>
                {{ rangeNames[range] }} · {{ number(totals.requests) }} 次请求
              </p>
            </div>
            <span class="chart-chip">API REQUESTS</span>
          </div>
          <UsageChart
            :data="data.timeseries"
            kind="requests"
          />
        </article>
        <article class="chart-card">
          <div class="chart-card-heading">
            <div>
              <h3>Token 使用趋势</h3>
              <p>
                输入 {{ compact(totals.prompt_tokens) }} · 输出
                {{ compact(totals.completion_tokens) }}
              </p>
            </div>
            <span class="chart-chip">TOKENS</span>
          </div>
          <UsageChart
            :data="data.timeseries"
            kind="tokens"
          />
        </article>
      </div>

      <div class="charts-grid">
        <UsageDistribution
          title="模型分布"
          :items="data.models"
        />
        <UsageDistribution
          title="端点分布"
          :items="data.endpoints"
        />
      </div>

      <section class="recent-card">
        <header class="recent-heading">
          <div>
            <h3>
              请求明细
              <span>{{ number(data.requests.total) }}</span>
            </h3>
            <p>每次 API 请求的结果与用量，点击展开详情。</p>
          </div>
          <button
            class="export-button"
            :disabled="!data.requests.items.length || loading"
            @click="exportPage"
          >
            <Download :size="13" />
            导出本页
          </button>
        </header>
        <div class="request-table-scroll">
          <table class="requests-table">
            <thead>
              <tr>
                <th class="expand-col" />
                <th>时间</th>
                <th>模型</th>
                <th>方式</th>
                <th>结果</th>
                <th class="tokens-col">
                  Token
                  <span>输入 / 输出</span>
                </th>
                <th class="duration-col">
                  耗时
                </th>
                <th class="duration-col">
                  首个输出
                </th>
              </tr>
            </thead>
            <tbody>
              <template
                v-for="item in data.requests.items"
                :key="item.id"
              >
                <tr
                  class="request-row"
                  :class="{ 'row-expanded': expanded === item.id }"
                  @click="expanded = expanded === item.id ? null : item.id"
                >
                  <td class="expand-col">
                    <button
                      class="row-expand"
                      :aria-label="`${expanded === item.id ? '收起' : '展开'}请求 ${item.id}`"
                      :aria-expanded="expanded === item.id"
                      @click.stop="
                        expanded = expanded === item.id ? null : item.id
                      "
                    >
                      <ChevronDown
                        v-if="expanded === item.id"
                        :size="13"
                      />
                      <ChevronRight
                        v-else
                        :size="13"
                      />
                    </button>
                  </td>
                  <td class="request-time">
                    {{ dateTime(item.started_at) }}
                  </td>
                  <td class="request-model">
                    <span :title="item.model">{{ item.model || '—' }}</span>
                    <small>
                      {{
                        item.endpoint.startsWith('/v1beta')
                          ? 'Gemini API'
                          : 'OpenAI 兼容'
                      }}
                    </small>
                  </td>
                  <td>
                    <span
                      class="stream-badge"
                      :class="{ stream: item.stream }"
                    >
                      {{ item.stream ? '流式' : '非流' }}
                    </span>
                  </td>
                  <td>
                    <span
                      class="status-badge"
                      :class="item.status"
                    >
                      <i />
                      {{ statuses[item.status] }}
                    </span>
                  </td>
                  <td class="token-cell">
                    <span class="input-token">
                      <ArrowUp :size="12" />
                      {{ token(item, 'prompt_tokens') }}
                    </span>
                    <span class="output-token">
                      <ArrowDown :size="12" />
                      {{ token(item, 'completion_tokens') }}
                    </span>
                  </td>
                  <td class="duration-cell">
                    {{ duration(item.duration_ms) }}
                  </td>
                  <td class="duration-cell ttft-cell">
                    {{ item.stream ? duration(item.ttft_ms) : '—' }}
                  </td>
                </tr>
                <tr
                  v-if="expanded === item.id"
                  class="request-details"
                >
                  <td colspan="8">
                    <dl>
                      <div class="request-id">
                        <dt>请求 ID</dt>
                        <dd>{{ item.id }}</dd>
                      </div>
                      <div class="request-endpoint">
                        <dt>调用端点</dt>
                        <dd>{{ item.endpoint }}</dd>
                      </div>
                      <div>
                        <dt title="流式响应内的错误使用上游结果码">
                          结果状态码
                        </dt>
                        <dd>{{ item.status_code ?? '—' }}</dd>
                      </div>
                      <div>
                        <dt>结束原因</dt>
                        <dd>{{ item.finish_reason || '—' }}</dd>
                      </div>
                      <div>
                        <dt>输入 Token</dt>
                        <dd>{{ token(item, 'prompt_tokens') }}</dd>
                      </div>
                      <div>
                        <dt>其中缓存命中</dt>
                        <dd>{{ token(item, 'cached_tokens') }}</dd>
                      </div>
                      <div>
                        <dt>输出 Token</dt>
                        <dd>{{ token(item, 'completion_tokens') }}</dd>
                      </div>
                      <div>
                        <dt>其中思考 Token</dt>
                        <dd>{{ token(item, 'reasoning_tokens') }}</dd>
                      </div>
                      <div>
                        <dt>总 Token</dt>
                        <dd>{{ token(item, 'total_tokens') }}</dd>
                      </div>
                    </dl>
                    <p
                      v-if="!item.usage_known"
                      class="unknown-note"
                    >
                      此请求尚未收到上游 Token 用量，显示为「—」。
                    </p>
                  </td>
                </tr>
              </template>
              <tr v-if="!data.requests.items.length">
                <td
                  colspan="8"
                  class="requests-empty"
                >
                  <Activity :size="24" />
                  <strong>此筛选下暂无请求</strong>
                  <span>发起一次模型调用后，记录会显示在这里。</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <footer class="table-footer">
          <span>
            共 {{ number(data.requests.total) }} 条
            <span class="desktop-hint">
              · 每页
              <select
                v-model.number="pageSize"
                aria-label="每页请求数"
              >
                <option :value="10">10</option>
                <option :value="20">20</option>
                <option :value="50">50</option>
              </select>
              条
            </span>
          </span>
          <div class="pagination">
            <button
              :disabled="page <= 1 || loading"
              aria-label="上一页"
              @click="page--"
            >
              <ChevronLeft :size="14" />
            </button>
            <span>{{ page }} / {{ totalPages }}</span>
            <button
              :disabled="page >= totalPages || loading"
              aria-label="下一页"
              @click="page++"
            >
              <ChevronRight :size="14" />
            </button>
          </div>
        </footer>
      </section>

      <div class="analytics-footnote">
        <p>
          时间按本机时区显示 · 明细保留 {{ data.retention_days }} 天，最多 10 万条。{{
            data.history_started_at
              ? `开始记录于 ${shortDate(data.history_started_at)}。`
              : '更新后开始记录明细。'
          }}
          升级前历史请查看下方原有模型调用统计。
        </p>
        <p>
          平均耗时含已结束请求；首个输出仅统计成功流式请求。输出含思考
          Token，缓存计入输入。
          <span v-if="lastUpdated">
            更新于
            {{ lastUpdated.toLocaleTimeString('zh-CN', { hour12: false }) }}
          </span>
        </p>
      </div>
    </template>

    <div
      v-else-if="loading"
      class="analytics-skeleton"
      aria-label="正在加载调用统计"
    >
      <div
        v-for="index in 4"
        :key="index"
      />
      <div class="skeleton-chart" />
      <div class="skeleton-chart" />
    </div>
  </section>
</template>

<style scoped>
.request-analytics {
  color: #0f172a;
  font-size: 13px;
  --muted: #64748b;
}
.demo-note {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 11px 15px;
  background: #eff6ff;
  border: 1px solid #dbeafe;
  border-radius: 10px;
  font-size: 11px;
  color: #3b82f6;
  margin-bottom: 22px;
  flex-wrap: wrap;
}
.demo-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #3b82f6;
}
.analytics-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  margin-bottom: 24px;
}
.section-eyebrow {
  font-size: 10px;
  letter-spacing: 1.7px;
  color: #94a3b8;
  font-weight: 600;
  margin-bottom: 7px;
}
.analytics-heading h2 {
  font-size: 24px;
  font-weight: 650;
  display: flex;
  align-items: center;
  gap: 14px;
  letter-spacing: -0.6px;
}
.analytics-heading p {
  font-size: 12px;
  color: #94a3b8;
  margin-top: 7px;
}
.live-label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  background: #ecfdf5;
  border: 1px solid #d1fae5;
  border-radius: 20px;
  padding: 3px 8px;
  color: #059669;
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0;
}
.live-label i {
  width: 5px;
  height: 5px;
  background: #10b981;
  border-radius: 50%;
}
.refresh-button,
.export-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  border: 1px solid #e2e8f0;
  background: white;
  border-radius: 9px;
  padding: 9px 12px;
  color: #475569;
  font-size: 11px;
  cursor: pointer;
  white-space: nowrap;
}
.refresh-button:hover,
.export-button:hover {
  background: #f8fafc;
}
.refresh-button:disabled,
.export-button:disabled {
  opacity: 0.45;
  cursor: default;
}
.spinning {
  animation: spin 1.2s linear infinite;
}
.analytics-toolbar {
  display: flex;
  gap: 16px;
  align-items: center;
  justify-content: space-between;
  border: 1px solid #e2e8f0;
  background: #fff;
  padding: 12px 15px;
  border-radius: 13px;
  margin-bottom: 18px;
}
.filter-controls {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
.filter-field {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 11px;
  color: #94a3b8;
}
.filter-field select,
.grain-field select {
  max-width: 200px;
  color: #334155;
  font-size: 11px;
  background: #f8fafc;
  border: 1px solid #eef2f6;
  border-radius: 7px;
  padding: 7px 23px 7px 9px;
  outline: none;
  cursor: pointer;
}
.filter-field select:focus,
.grain-field select:focus {
  border-color: #93c5fd;
}
.grain-field {
  display: flex;
  align-items: center;
  gap: 7px;
  color: #94a3b8;
  font-size: 11px;
  white-space: nowrap;
}
.clear-filter {
  display: flex;
  align-items: center;
  gap: 3px;
  color: #64748b;
  font-size: 10px;
  cursor: pointer;
}
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 14px;
}
.metric-card {
  border: 1px solid #e2e8f0;
  border-radius: 15px;
  background: #fff;
  padding: 19px 20px;
}
.metric-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  color: #64748b;
  font-size: 11px;
}
.metric-top svg {
  color: #94a3b8;
}
.metric-value {
  font-size: 30px;
  font-weight: 600;
  line-height: 1.3;
  letter-spacing: -1px;
  margin: 13px 0 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.metric-value small {
  font-size: 10px;
  color: #94a3b8;
  font-weight: 400;
  letter-spacing: 0;
  margin-left: 6px;
}
.metric-bottom {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 10px;
  color: #94a3b8;
  min-height: 16px;
  flex-wrap: wrap;
}
.metric-bottom > span {
  display: flex;
  align-items: center;
  gap: 3px;
}
.metric-bottom .metric-subvalue {
  margin-left: auto;
  color: #475569;
  font-weight: 500;
}
.metric-bottom .success-text,
.success-text {
  color: #059669;
}
.metric-bottom .separator {
  color: #cbd5e1;
}
.runtime-strip {
  display: flex;
  gap: 25px;
  align-items: center;
  padding: 12px 2px 22px;
  font-size: 11px;
  color: #94a3b8;
  flex-wrap: wrap;
}
.runtime-strip > span {
  display: flex;
  align-items: center;
  gap: 7px;
}
.runtime-strip strong {
  font-weight: 500;
  color: #475569;
  font-variant-numeric: tabular-nums;
}
.runtime-strip .uptime {
  margin-left: auto;
}
.runtime-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #cbd5e1;
}
.runtime-dot.busy {
  background: #3b82f6;
  box-shadow: 0 0 0 3px #dbeafe;
}
.charts-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 18px;
  margin-bottom: 18px;
}
.chart-card {
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 18px;
  padding: 22px 22px 12px;
  min-width: 0;
}
.chart-card-heading {
  display: flex;
  justify-content: space-between;
  align-items: start;
  gap: 8px;
  margin-bottom: 20px;
}
.chart-card h3,
.recent-heading h3 {
  font-size: 14px;
  font-weight: 600;
}
.chart-card p,
.recent-heading p {
  font-size: 11px;
  color: #94a3b8;
  margin-top: 6px;
}
.chart-chip {
  font-size: 8px;
  letter-spacing: 1px;
  border: 1px solid #e2e8f0;
  border-radius: 5px;
  color: #94a3b8;
  padding: 4px 5px;
  white-space: nowrap;
}
.recent-card {
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 18px;
  overflow: hidden;
  margin-bottom: 27px;
}
.recent-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 22px;
  gap: 10px;
}
.recent-heading h3 {
  display: flex;
  align-items: center;
  gap: 8px;
}
.recent-heading h3 span {
  font-size: 10px;
  font-weight: 500;
  color: #64748b;
  background: #f1f5f9;
  padding: 2px 6px;
  border-radius: 5px;
}
.export-button {
  padding: 7px 10px;
  font-size: 10px;
}
.request-table-scroll {
  overflow-x: auto;
}
.requests-table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  white-space: nowrap;
  font-size: 11px;
}
.requests-table thead {
  color: #94a3b8;
  background: #f8fafc;
  border-top: 1px solid #f1f5f9;
  border-bottom: 1px solid #e2e8f0;
}
.requests-table th {
  font-size: 10px;
  font-weight: 500;
  padding: 12px 13px;
}
.requests-table th span {
  font-size: 9px;
  color: #cbd5e1;
  margin-left: 4px;
}
.requests-table td {
  padding: 13px;
}
.requests-table .expand-col {
  width: 30px;
  padding-left: 17px;
  padding-right: 0;
}
.row-expand {
  color: #94a3b8;
  padding: 3px;
  cursor: pointer;
  border-radius: 4px;
}
.row-expand:hover {
  color: #334155;
  background: #e2e8f0;
}
.request-row {
  border-bottom: 1px solid #f1f5f9;
  cursor: pointer;
  transition: background 0.15s;
}
.request-row:hover,
.request-row.row-expanded {
  background: #f8fafc;
}
.request-time {
  font-variant-numeric: tabular-nums;
  color: #64748b;
}
.request-model > span {
  display: block;
  max-width: 210px;
  overflow: hidden;
  text-overflow: ellipsis;
  font-weight: 500;
  color: #334155;
}
.request-model small {
  display: block;
  color: #94a3b8;
  font-size: 9px;
  margin-top: 3px;
}
.stream-badge {
  display: inline-flex;
  background: #f1f5f9;
  color: #64748b;
  padding: 3px 6px;
  border-radius: 5px;
  font-size: 10px;
}
.stream-badge.stream {
  background: #eff6ff;
  color: #3b82f6;
}
.status-badge {
  display: inline-flex;
  gap: 5px;
  align-items: center;
  border-radius: 5px;
  padding: 3px 6px;
  font-size: 10px;
}
.status-badge i {
  height: 4px;
  width: 4px;
  border-radius: 50%;
  background: currentColor;
}
.status-badge.success {
  color: #059669;
  background: #ecfdf5;
}
.status-badge.error {
  color: #e11d48;
  background: #fff1f2;
}
.status-badge.rate_limited {
  color: #d97706;
  background: #fffbeb;
}
.status-badge.cancelled {
  color: #64748b;
  background: #f1f5f9;
}
.status-badge.active {
  color: #2563eb;
  background: #eff6ff;
}
.token-cell {
  font-variant-numeric: tabular-nums;
  text-align: right;
}
.token-cell > span {
  display: inline-flex;
  gap: 3px;
  align-items: center;
  min-width: 65px;
  justify-content: flex-end;
}
.token-cell .input-token {
  color: #475569;
}
.token-cell .input-token svg {
  color: #3b82f6;
}
.token-cell .output-token {
  color: #475569;
  margin-left: 8px;
}
.token-cell .output-token svg {
  color: #10b981;
}
.duration-cell,
.duration-col {
  text-align: right !important;
  font-variant-numeric: tabular-nums;
  color: #475569;
}
.tokens-col {
  text-align: right !important;
}
.duration-cell:last-child,
.duration-col:last-child {
  padding-right: 22px;
}
.ttft-cell {
  color: #94a3b8;
}
.request-details {
  background: #f8fafc;
  border-bottom: 1px solid #e2e8f0;
}
.request-details td {
  padding: 18px 25px 20px 45px;
}
.request-details dl {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px 22px;
  white-space: normal;
}
.request-details dt {
  font-size: 10px;
  color: #94a3b8;
  margin-bottom: 4px;
}
.request-details dd {
  font-size: 11px;
  color: #475569;
  font-variant-numeric: tabular-nums;
  word-break: break-all;
}
.request-id,
.request-endpoint {
  grid-column: span 2;
}
.request-id dd {
  font-family: ui-monospace, monospace;
}
.unknown-note {
  font-size: 10px;
  color: #94a3b8;
  margin-top: 16px;
}
.requests-empty {
  padding: 45px !important;
  text-align: center;
  color: #94a3b8;
  white-space: normal;
}
.requests-empty svg {
  margin: 0 auto 12px;
  color: #cbd5e1;
}
.requests-empty strong,
.requests-empty span {
  display: block;
}
.requests-empty strong {
  font-size: 12px;
  color: #64748b;
  font-weight: 500;
}
.requests-empty span {
  font-size: 11px;
  margin-top: 7px;
}
.table-footer {
  padding: 13px 22px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: #94a3b8;
  font-size: 10px;
  border-top: 1px solid #f1f5f9;
  gap: 8px;
}
.desktop-hint select {
  padding: 2px 4px;
  color: #64748b;
  background: transparent;
  cursor: pointer;
}
.pagination {
  display: flex;
  gap: 12px;
  align-items: center;
}
.pagination button {
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 5px;
  color: #475569;
  cursor: pointer;
}
.pagination button:disabled {
  opacity: 0.3;
  cursor: default;
}
.analytics-footnote {
  font-size: 10px;
  line-height: 1.8;
  color: #94a3b8;
  margin-top: 4px;
}
.analytics-footnote p span {
  margin-left: 12px;
}
.analytics-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 15px;
  color: #be123c;
  background: #fff1f2;
  border: 1px solid #ffe4e6;
  border-radius: 10px;
  font-size: 11px;
  margin-bottom: 18px;
}
.analytics-error button {
  text-decoration: underline;
  white-space: nowrap;
  cursor: pointer;
}
.analytics-skeleton {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}
.analytics-skeleton > div {
  height: 140px;
  border-radius: 15px;
  background: linear-gradient(100deg, #e2e8f0 30%, #f1f5f9 50%, #e2e8f0 70%);
  background-size: 300% 100%;
  animation: shimmer 2s infinite;
}
.analytics-skeleton .skeleton-chart {
  grid-column: span 2;
  height: 280px;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
@keyframes shimmer {
  to {
    background-position: -150% 0;
  }
}
@media (min-width: 1600px) {
  .metric-value {
    font-size: 34px;
  }
}
@media (max-width: 1200px) {
  .metrics-grid {
    gap: 10px;
  }
  .metric-card {
    padding: 17px 15px;
  }
  .metric-value {
    font-size: 27px;
  }
  .charts-grid {
    gap: 14px;
  }
  .chart-card {
    padding: 19px 15px 10px;
  }
  .token-cell > span {
    min-width: 55px;
  }
  .request-model > span {
    max-width: 170px;
  }
  .filter-controls {
    gap: 9px;
  }
  .filter-field select {
    max-width: 170px;
  }
}
@media (max-width: 900px) {
  .metrics-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .analytics-toolbar {
    align-items: flex-start;
  }
  .filter-field {
    flex-wrap: wrap;
  }
  .runtime-strip {
    gap: 17px;
  }
  .runtime-strip .uptime {
    margin-left: 0;
  }
  .chart-chip {
    display: none;
  }
  .charts-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .request-details dl {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .analytics-heading h2 {
    font-size: 22px;
  }
}
@media (max-width: 500px) {
  .analytics-heading {
    margin-bottom: 19px;
  }
  .analytics-heading h2 {
    font-size: 20px;
    gap: 8px;
  }
  .analytics-heading p {
    font-size: 11px;
  }
  .refresh-button {
    padding: 8px;
    font-size: 10px;
  }
  .refresh-button span {
    display: none;
  }
  .section-eyebrow {
    font-size: 9px;
  }
  .analytics-toolbar {
    gap: 10px;
    padding: 12px;
  }
  .filter-controls {
    gap: 8px;
    width: 100%;
  }
  .filter-field {
    gap: 5px;
    flex: 1 1 100%;
  }
  .filter-field > span {
    width: 25px;
  }
  .filter-field > svg {
    display: none;
  }
  .filter-field select {
    flex: 1;
    max-width: none;
  }
  .grain-field {
    font-size: 10px;
    align-self: flex-start;
  }
  .grain-field > span {
    display: none;
  }
  .grain-field select {
    padding: 7px;
  }
  .metric-card {
    padding: 15px 13px;
    border-radius: 12px;
  }
  .metric-top {
    font-size: 10px;
  }
  .metric-top svg {
    width: 14px;
  }
  .metric-value {
    font-size: 26px;
    margin: 11px 0;
  }
  .metric-value small {
    font-size: 9px;
    margin-left: 4px;
  }
  .metric-bottom {
    font-size: 9px;
    gap: 5px;
  }
  .runtime-strip {
    font-size: 10px;
    gap: 12px 18px;
    padding-bottom: 19px;
  }
  .chart-card {
    border-radius: 14px;
    padding: 18px 12px 9px;
  }
  .chart-card-heading {
    padding: 0 5px;
    margin-bottom: 16px;
  }
  .recent-heading {
    padding: 18px;
  }
  .recent-heading p {
    font-size: 10px;
  }
  .recent-heading h3 {
    font-size: 13px;
  }
  .export-button {
    font-size: 9px;
    padding: 6px 7px;
  }
  .requests-table td {
    padding: 11px;
  }
  .requests-table th {
    padding: 10px 11px;
  }
  .request-details td {
    padding: 16px 20px;
  }
  .table-footer {
    padding: 12px 16px;
  }
  .desktop-hint {
    display: none;
  }
  .analytics-footnote {
    font-size: 9px;
  }
  .analytics-skeleton {
    grid-template-columns: repeat(2, 1fr);
  }
  .analytics-skeleton .skeleton-chart {
    grid-column: span 2;
  }
  .demo-note {
    font-size: 10px;
  }
  .live-label {
    font-size: 9px;
    padding: 3px 6px;
  }
}
@media (prefers-reduced-motion: reduce) {
  .spinning,
  .analytics-skeleton > div {
    animation: none;
  }
}
</style>
