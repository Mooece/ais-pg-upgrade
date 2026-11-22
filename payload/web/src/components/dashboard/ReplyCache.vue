<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { RefreshCw, RotateCcw } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import { usePolling } from '@/composables/usePolling.ts'
import {
  armReplyReplay,
  cancelReplyReplay,
  getReplyCache,
  setReplyCacheLimit,
} from '@/api/replyCache.ts'
import type { ReplyCacheState } from '@/api/replyCache.ts'

const state = ref<ReplyCacheState | null>(null)
const draftLimit = ref(0)
const busy = ref(false)
const error = ref('')
const changed = computed(() => state.value !== null && state.value.limit !== draftLimit.value)
const pending = computed(() => state.value?.entries.find((item) => item.id === state.value?.pending_id))
const dateTime = (value: string) => new Date(value).toLocaleString('zh-CN', {
  dateStyle: 'short',
  timeStyle: 'medium',
})
let loader: AbortController | null = null

async function update(operation: () => Promise<ReplyCacheState>, keepDraft: boolean) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const next = await operation()
    if (!keepDraft || !changed.value) draftLimit.value = next.limit
    state.value = next
  } catch (err: unknown) {
    if (!loader?.signal.aborted) {
      error.value = err instanceof Error ? err.message : '操作失败，请刷新后重试。'
    }
  } finally {
    busy.value = false
  }
}

async function refresh() {
  if (busy.value) return
  const controller = new AbortController()
  loader = controller
  await update(() => getReplyCache(controller.signal), true)
  loader = null
}

function saveLimit() {
  if (!state.value?.available || !changed.value) return
  const limit = draftLimit.value
  return update(() => setReplyCacheLimit(limit), false)
}

usePolling(refresh, 10000)
onUnmounted(() => loader?.abort())
</script>

<template>
  <section
    aria-labelledby="reply-cache-title"
    class="overflow-hidden rounded-[18px] border border-slate-200 bg-white"
  >
    <header class="flex flex-wrap items-center justify-between gap-3 p-5">
      <div>
        <h2
          id="reply-cache-title"
          class="flex items-center gap-2 text-sm font-semibold text-slate-900"
        >
          <RotateCcw
            class="size-4 text-brand-500"
            aria-hidden="true"
          />
          回复缓存
        </h2>
        <p class="mt-1.5 text-xs leading-relaxed text-slate-500">
          酒馆没收到回复时，选中下方一轮，再回到酒馆重试原请求。
        </p>
      </div>
      <Button
        size="sm"
        :disabled="busy"
        @click="refresh"
      >
        <RefreshCw
          class="size-3.5"
          aria-hidden="true"
        />
        刷新缓存
      </Button>
    </header>

    <p
      v-if="error"
      role="alert"
      class="mx-5 mb-4 rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-800"
    >
      {{ error }}{{ state ? ' 当前显示上次读取的数据。' : '' }}
    </p>

    <p
      v-if="state && !state.available"
      class="border-t border-slate-100 px-5 py-4 text-xs text-slate-600"
    >
      设置面板密码并配置 API 密钥后，即可启用回复缓存。
    </p>
    <template v-else-if="state">
      <form
        class="flex flex-wrap items-center gap-x-3 gap-y-2 border-y border-slate-100 bg-slate-50/70 px-5 py-3"
        @submit.prevent="saveLimit"
      >
        <label class="inline-flex items-center gap-2 text-xs font-medium text-slate-700">
          保留轮数
          <select
            v-model="draftLimit"
            class="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-slate-800"
          >
            <option :value="0">关闭</option>
            <option
              v-for="count in 20"
              :key="count"
              :value="count"
            >{{ count }} 轮</option>
          </select>
        </label>
        <Button
          type="submit"
          size="sm"
          :disabled="busy || !changed"
        >
          保存
        </Button>
        <p class="text-xs text-slate-500">
          {{ draftLimit === 0 && changed ? '关闭会清空已有缓存。' : '超出保留轮数时，最早的回复会被移除。' }}
        </p>
      </form>

      <div
        v-if="state.pending_id"
        class="mx-5 mt-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-brand-100 bg-brand-50 px-3 py-2"
      >
        <div
          role="status"
          class="text-xs leading-relaxed text-slate-700"
        >
          <strong class="text-brand-700">待返回一次</strong>
          <span v-if="pending"> · {{ pending.model }} · {{ dateTime(pending.created_at) }}</span>
          <p>使用生成这条回复的 API 密钥发起下一次 OpenAI 对话请求，即可取回这条缓存。</p>
        </div>
        <Button
          size="sm"
          :disabled="busy"
          @click="update(cancelReplyReplay, true)"
        >
          取消返回
        </Button>
      </div>

      <ul
        v-if="state.entries.length"
        class="divide-y divide-slate-100"
        aria-label="已缓存回复"
      >
        <li
          v-for="entry in state.entries"
          :key="entry.id"
          class="flex flex-wrap items-center justify-between gap-3 px-5 py-3 sm:flex-nowrap"
        >
          <div class="min-w-0 flex-1 basis-full sm:basis-auto">
            <div class="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
              <span class="break-all font-medium text-slate-800">{{ entry.model }}</span>
              <time
                :datetime="entry.created_at"
                class="tabular-nums text-slate-500"
              >
                {{ dateTime(entry.created_at) }}
              </time>
            </div>
            <p class="mt-1 line-clamp-2 whitespace-pre-wrap break-words text-xs leading-relaxed text-slate-600">
              {{ entry.preview || '这轮回复没有可预览的文字。' }}
            </p>
          </div>
          <Button
            size="sm"
            class="shrink-0"
            :variant="entry.id === state.pending_id ? 'secondary' : 'outline'"
            :disabled="busy || entry.id === state.pending_id"
            @click="update(() => armReplyReplay(entry.id), true)"
          >
            {{ entry.id === state.pending_id ? '等待请求' : '返回缓存' }}
          </Button>
        </li>
      </ul>
      <p
        v-else
        class="px-5 py-5 text-xs text-slate-500"
      >
        {{ state.limit ? '还没有缓存。新的 OpenAI 兼容回复生成完成后会显示在这里。' : '回复缓存已关闭。选择保留轮数并保存即可启用。' }}
      </p>
    </template>
    <p
      v-else-if="!error"
      role="status"
      class="px-5 pb-5 text-xs text-slate-500"
    >
      正在加载回复缓存…
    </p>
  </section>
</template>
