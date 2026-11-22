import { request } from './client.ts'

export interface CachedReply {
  id: string
  model: string
  created_at: string
  preview: string
}

export interface ReplyCacheState {
  available: boolean
  limit: number
  entries: CachedReply[]
  pending_id: string | null
}

const endpoint = '/api/reply-cache'

export function getReplyCache(signal: AbortSignal) {
  return request<ReplyCacheState>(endpoint, { signal, cache: 'no-store' })
}

export function setReplyCacheLimit(limit: number) {
  return request<ReplyCacheState>(endpoint, {
    method: 'PUT',
    body: JSON.stringify({ limit }),
  })
}

export function armReplyReplay(id: string) {
  return request<ReplyCacheState>(`${endpoint}/replay`, {
    method: 'POST',
    body: JSON.stringify({ id }),
  })
}

export function cancelReplyReplay() {
  return request<ReplyCacheState>(`${endpoint}/replay`, { method: 'DELETE' })
}
