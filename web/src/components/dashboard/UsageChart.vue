<script setup lang="ts">
import { computed, ref, useId } from 'vue'
import type { AnalyticsBucket } from '@/types/analytics.ts'
import { compact, number } from './analyticsFormat.ts'

const props = defineProps<{
  data: AnalyticsBucket[]
  kind: 'requests' | 'tokens'
}>()

const selected = ref<number | null>(null)
const gradient = `usage-${useId().replace(/:/g, '')}`
const width = 640
const left = 52
const right = 18
const top = 18
const bottom = 190
const plotWidth = width - left - right
const metric = (item: AnalyticsBucket) =>
  props.kind === 'requests'
    ? item.requests
    : Math.max(0, item.prompt_tokens) + Math.max(0, item.completion_tokens)
const maxValue = computed(() => {
  const max = Math.max(2, ...props.data.map(metric))
  const unit = 10 ** Math.floor(Math.log10(max / 2))
  return Math.ceil(max / 2 / unit) * unit * 2
})
const x = (index: number) =>
  left + ((index + 0.5) * plotWidth) / Math.max(1, props.data.length)
const y = (value: number) => bottom - (value / maxValue.value) * (bottom - top)
const line = computed(() =>
  props.data
    .map(
      (item, index) =>
        `${index === 0 ? 'M' : 'L'}${x(index)},${y(item.requests)}`,
    )
    .join(' '),
)
const area = computed(() =>
  props.data.length
    ? `${line.value} L${x(props.data.length - 1)},${bottom} L${x(0)},${bottom} Z`
    : '',
)
const tickIndices = computed(() =>
  [
    ...new Set([
      0,
      Math.floor((props.data.length - 1) / 3),
      Math.floor(((props.data.length - 1) * 2) / 3),
      props.data.length - 1,
    ]),
  ].filter((i) => i >= 0),
)
const hovered = computed(() =>
  selected.value == null ? null : (props.data[selected.value] ?? null),
)
const tooltipLeft = computed(() =>
  selected.value == null
    ? '50%'
    : `${Math.max(26, Math.min(73, (x(selected.value) / width) * 100))}%`,
)
const barWidth = computed(() =>
  Math.max(
    1,
    Math.min(24, (plotWidth / Math.max(1, props.data.length)) * 0.66),
  ),
)
function hover(event: MouseEvent) {
  const rect = (event.currentTarget as SVGElement).getBoundingClientRect()
  const local = ((event.clientX - rect.left) / rect.width) * width
  selected.value = Math.max(
    0,
    Math.min(
      props.data.length - 1,
      Math.floor(((local - left) / plotWidth) * props.data.length),
    ),
  )
}

function move(event: KeyboardEvent) {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
  event.preventDefault()
  const delta = event.key === 'ArrowLeft' ? -1 : 1
  selected.value = Math.max(
    0,
    Math.min(props.data.length - 1, (selected.value ?? 0) + delta),
  )
}
</script>

<template>
  <div class="usage-chart">
    <div
      v-if="kind === 'tokens'"
      class="chart-legend"
    >
      <span>
        <i style="background: #60a5fa" />
        输入
      </span>
      <span>
        <i style="background: #2563eb" />
        输出
      </span>
    </div>
    <div
      v-else
      class="chart-legend"
    >
      <span>
        <i style="background: #3b82f6" />
        请求次数
      </span>
    </div>
    <div class="chart-stage">
      <svg
        viewBox="0 0 640 220"
        role="img"
        :aria-label="
          kind === 'tokens'
            ? 'Token 用量趋势，使用左右方向键查看各时间段'
            : 'API 请求次数趋势，使用左右方向键查看各时间段'
        "
        tabindex="0"
        @mousemove="hover"
        @mouseleave="selected = null"
        @focus="selected = 0"
        @blur="selected = null"
        @keydown="move"
        @click="hover"
      >
        <defs>
          <linearGradient
            :id="gradient"
            x1="0"
            y1="0"
            x2="0"
            y2="1"
          >
            <stop
              offset="0%"
              stop-color="#3b82f6"
              stop-opacity=".24"
            />
            <stop
              offset="100%"
              stop-color="#3b82f6"
              stop-opacity=".025"
            />
          </linearGradient>
        </defs>
        <g
          v-for="tick in [0, 0.5, 1]"
          :key="tick"
        >
          <line
            :x1="left"
            :x2="width - right"
            :y1="y(maxValue * tick)"
            :y2="y(maxValue * tick)"
            stroke="#e2e8f0"
          />
          <text
            :x="left - 9"
            :y="y(maxValue * tick) + 4"
            text-anchor="end"
            class="axis-label"
          >
            {{ compact(maxValue * tick) }}
          </text>
        </g>
        <template v-if="kind === 'requests'">
          <path
            :d="area"
            :fill="`url(#${gradient})`"
          />
          <path
            :d="line"
            fill="none"
            stroke="#3b82f6"
            stroke-width="2.5"
            stroke-linejoin="round"
            stroke-linecap="round"
          />
          <circle
            v-if="data.length === 1"
            :cx="x(0)"
            :cy="y(data[0]?.requests ?? 0)"
            r="4"
            fill="#3b82f6"
          />
        </template>
        <template v-else>
          <g
            v-for="(item, index) in data"
            :key="item.bucket"
          >
            <rect
              :x="x(index) - barWidth / 2"
              :y="y(item.prompt_tokens)"
              :width="barWidth"
              :height="Math.max(0, bottom - y(item.prompt_tokens))"
              fill="#60a5fa"
              rx="1"
            />
            <rect
              :x="x(index) - barWidth / 2"
              :y="y(item.prompt_tokens + item.completion_tokens)"
              :width="barWidth"
              :height="
                Math.max(
                  0,
                  y(item.prompt_tokens) -
                    y(item.prompt_tokens + item.completion_tokens),
                )
              "
              fill="#2563eb"
              rx="1"
            />
          </g>
        </template>
        <line
          v-if="selected !== null && data.length"
          :x1="x(selected)"
          :x2="x(selected)"
          :y1="top"
          :y2="bottom"
          stroke="#94a3b8"
          stroke-dasharray="4 4"
        />
        <circle
          v-if="hovered && kind === 'requests'"
          :cx="x(selected ?? 0)"
          :cy="y(hovered.requests)"
          r="4"
          fill="#3b82f6"
          stroke="white"
          stroke-width="2"
        />
        <text
          v-for="index in tickIndices"
          :key="index"
          :x="x(index)"
          y="212"
          text-anchor="middle"
          class="axis-label"
        >
          {{ data[index]?.label }}
        </text>
      </svg>
      <div
        v-if="hovered"
        class="chart-tooltip"
        :style="{ left: tooltipLeft }"
        role="status"
      >
        <strong>{{ hovered.label }}</strong>
        <template v-if="kind === 'tokens'">
          <span>
            输入
            <b>{{ number(hovered.prompt_tokens) }}</b>
          </span>
          <span>
            输出
            <b>{{ number(hovered.completion_tokens) }}</b>
          </span>
          <span class="tooltip-total">
            合计
            <b>{{ number(hovered.total_tokens) }}</b>
          </span>
        </template>
        <template v-else>
          <span>
            请求次数
            <b>{{ number(hovered.requests) }}</b>
          </span>
          <span>
            成功
            <b>{{ number(hovered.success) }}</b>
          </span>
          <span>
            失败
            <b>{{ number(hovered.errors) }}</b>
          </span>
          <span v-if="hovered.rate_limited">
            限流
            <b>{{ number(hovered.rate_limited) }}</b>
          </span>
          <span v-if="hovered.cancelled">
            已中断
            <b>{{ number(hovered.cancelled) }}</b>
          </span>
        </template>
      </div>
      <div
        v-if="!data.some((item) => metric(item) > 0)"
        class="chart-empty"
      >
        {{ kind === 'tokens' ? '此时段暂无 Token 用量' : '此时段暂无请求' }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.usage-chart {
  min-width: 0;
}
.chart-legend {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  min-height: 24px;
  margin: 0 0 6px 4px;
  color: #64748b;
  font-size: 11px;
}
.chart-legend span {
  display: flex;
  align-items: center;
  gap: 6px;
}
.chart-legend i {
  width: 8px;
  height: 8px;
  border-radius: 2px;
}
.chart-stage {
  position: relative;
}
.chart-stage svg {
  display: block;
  width: 100%;
  outline-offset: 3px;
  border-radius: 8px;
}
.axis-label {
  font-size: 11px;
  fill: #94a3b8;
}
.chart-tooltip {
  position: absolute;
  top: 5%;
  transform: translateX(-50%);
  pointer-events: none;
  background: rgba(255, 255, 255, 0.97);
  border: 1px solid #e2e8f0;
  box-shadow: 0 6px 24px #0f172a12;
  border-radius: 12px;
  padding: 12px 14px;
  min-width: 190px;
  font-size: 11px;
  color: #64748b;
  z-index: 5;
}
.chart-tooltip strong {
  display: block;
  color: #0f172a;
  margin-bottom: 7px;
  font-size: 12px;
}
.chart-tooltip span {
  display: flex;
  gap: 16px;
  justify-content: space-between;
  line-height: 1.9;
}
.chart-tooltip b {
  color: #334155;
  font-variant-numeric: tabular-nums;
  font-weight: 500;
}
.tooltip-total {
  border-top: 1px solid #f1f5f9;
  margin-top: 4px;
  padding-top: 4px;
}
.chart-empty {
  position: absolute;
  inset: 30% 12% 26% 16%;
  display: flex;
  justify-content: center;
  align-items: center;
  color: #94a3b8;
  font-size: 12px;
  pointer-events: none;
}
@media (max-width: 600px) {
  .chart-legend {
    gap: 10px;
    font-size: 10px;
  }
  .axis-label {
    font-size: 12px;
  }
  .chart-tooltip {
    top: 0;
    min-width: 170px;
  }
}
</style>
