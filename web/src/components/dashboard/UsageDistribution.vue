<script setup lang="ts">
import { computed, ref } from 'vue'
import type { AnalyticsDistribution } from '@/types/analytics.ts'
import { compact, number, duration } from './analyticsFormat.ts'

const props = defineProps<{ title: string; items: AnalyticsDistribution[] }>()
const metric = ref<'total_tokens' | 'requests'>('total_tokens')
const active = ref<number | null>(null)
const colors = [
  '#3b82f6',
  '#14b8a6',
  '#8b5cf6',
  '#f59e0b',
  '#ec4899',
  '#64748b',
]
const total = computed(() =>
  props.items.reduce((sum, item) => sum + item[metric.value], 0),
)
const segments = computed(() => {
  let offset = 0
  return props.items.map((item, index) => {
    const portion = total.value ? (item[metric.value] / total.value) * 100 : 0
    const segment = {
      name: item.name,
      portion,
      offset,
      color: colors[index % colors.length],
    }
    offset += portion
    return segment
  })
})
const current = computed(() =>
  active.value == null ? null : (props.items[active.value] ?? null),
)
</script>

<template>
  <section class="distribution-card">
    <header>
      <h3>{{ title }}</h3>
      <div
        class="metric-tabs"
        role="group"
        :aria-label="`${title}统计方式`"
      >
        <button
          :class="{ selected: metric === 'total_tokens' }"
          @click="metric = 'total_tokens'"
        >
          Token
        </button>
        <button
          :class="{ selected: metric === 'requests' }"
          @click="metric = 'requests'"
        >
          请求
        </button>
      </div>
    </header>
    <div class="distribution-body">
      <div class="donut-wrap">
        <svg
          viewBox="0 0 160 160"
          role="img"
          :aria-label="`${title}，按${metric === 'requests' ? '请求次数' : 'Token'}统计`"
        >
          <circle
            cx="80"
            cy="80"
            r="59"
            fill="none"
            stroke="#f1f5f9"
            stroke-width="22"
          />
          <circle
            v-for="(segment, index) in segments"
            :key="segment.name"
            cx="80"
            cy="80"
            r="59"
            fill="none"
            :stroke="segment.color"
            :stroke-opacity="active !== null && active !== index ? 0.25 : 1"
            stroke-width="22"
            pathLength="100"
            :stroke-dasharray="`${segment.portion} ${100 - segment.portion}`"
            :stroke-dashoffset="-segment.offset"
            transform="rotate(-90 80 80)"
            @mouseenter="active = index"
            @mouseleave="active = null"
          />
          <text
            x="80"
            y="76"
            text-anchor="middle"
            class="donut-number"
          >
            {{ compact(current ? current[metric] : total) }}
          </text>
          <text
            x="80"
            y="97"
            text-anchor="middle"
            class="donut-unit"
          >
            {{
              current
                ? `${total ? ((current[metric] / total) * 100).toFixed(1) : 0}%`
                : metric === 'requests'
                  ? '次请求'
                  : 'Tokens'
            }}
          </text>
        </svg>
      </div>
      <div class="distribution-list">
        <div class="distribution-head">
          <span>名称</span>
          <span>请求</span>
          <span>Token</span>
        </div>
        <div
          v-for="(item, index) in items"
          :key="item.name"
          class="distribution-row"
          :class="{ 'is-active': active === index }"
          tabindex="0"
          :title="`${item.name} · 平均耗时 ${duration(item.avg_duration_ms)}`"
          @mouseenter="active = index"
          @mouseleave="active = null"
          @focus="active = index"
          @blur="active = null"
        >
          <span class="distribution-name">
            <i :style="{ background: colors[index % colors.length] }" />
            <span>{{ item.name }}</span>
          </span>
          <span>{{ number(item.requests) }}</span>
          <span>{{ compact(item.total_tokens) }}</span>
        </div>
        <p
          v-if="!items.length"
          class="distribution-empty"
        >
          暂无分布数据
        </p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.distribution-card {
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 18px;
  padding: 22px;
  min-width: 0;
}
.distribution-card header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  gap: 8px;
}
.distribution-card h3 {
  font-size: 14px;
  color: #0f172a;
  font-weight: 600;
}
.metric-tabs {
  display: flex;
  background: #f1f5f9;
  border-radius: 7px;
  padding: 3px;
  font-size: 10px;
}
.metric-tabs button {
  padding: 4px 9px;
  color: #64748b;
  border-radius: 5px;
  cursor: pointer;
}
.metric-tabs button.selected {
  background: white;
  color: #334155;
  box-shadow: 0 1px 3px #0f172a14;
}
.distribution-body {
  display: flex;
  align-items: center;
  gap: 20px;
}
.donut-wrap {
  flex: 0 0 140px;
}
.donut-wrap svg {
  width: 100%;
  display: block;
}
.donut-number {
  font-size: 22px;
  font-weight: 600;
  fill: #0f172a;
  font-variant-numeric: tabular-nums;
}
.donut-unit {
  font-size: 11px;
  fill: #94a3b8;
}
.distribution-list {
  flex: 1;
  min-width: 0;
  max-height: 200px;
  overflow: auto;
}
.distribution-head,
.distribution-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 48px 62px;
  align-items: center;
  gap: 8px;
  font-size: 11px;
}
.distribution-head {
  color: #94a3b8;
  border-bottom: 1px solid #f1f5f9;
  padding: 0 0 9px;
}
.distribution-head span:not(:first-child),
.distribution-row > span:not(:first-child) {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.distribution-row {
  padding: 10px 0;
  border-bottom: 1px solid #f1f5f9;
  color: #64748b;
  outline-offset: -1px;
  border-radius: 3px;
}
.distribution-row.is-active {
  background: #f8fafc;
}
.distribution-name {
  display: flex;
  gap: 7px;
  align-items: center;
  color: #334155;
  min-width: 0;
}
.distribution-name i {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
}
.distribution-name > span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.distribution-empty {
  font-size: 12px;
  color: #94a3b8;
  text-align: center;
  padding: 24px 0;
}
@media (max-width: 1200px) {
  .distribution-body {
    gap: 10px;
  }
  .donut-wrap {
    flex-basis: 110px;
  }
  .distribution-card {
    padding: 18px;
  }
}
@media (max-width: 400px) {
  .distribution-body {
    flex-direction: column;
  }
  .donut-wrap {
    flex-basis: auto;
    width: 140px;
  }
  .distribution-list {
    width: 100%;
  }
}
</style>
