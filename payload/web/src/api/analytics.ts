import { request } from './client.ts'
import type { AnalyticsQuery, AnalyticsResponse } from '@/types/analytics.ts'

export function getAnalytics(
  query: AnalyticsQuery,
  signal?: AbortSignal,
): Promise<AnalyticsResponse> {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== '') params.set(key, String(value))
  }
  return request<AnalyticsResponse>(`/stats/analytics?${params}`, { signal })
}
