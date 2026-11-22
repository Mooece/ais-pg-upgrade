export type AnalyticsRange = 'today' | '7d' | '30d' | 'month' | 'last_month'
export type RequestStatus =
  'success' | 'error' | 'rate_limited' | 'cancelled' | 'active'

export interface AnalyticsBucket {
  bucket: string
  label: string
  requests: number
  success: number
  errors: number
  rate_limited?: number
  cancelled?: number
  prompt_tokens: number
  completion_tokens: number
  reasoning_tokens: number
  cached_tokens: number
  total_tokens: number
  avg_duration_ms: number | null
}

export interface AnalyticsDistribution {
  name: string
  requests: number
  success: number
  errors: number
  total_tokens: number
  avg_duration_ms: number | null
}

export interface AnalyticsModel extends AnalyticsDistribution {
  prompt_tokens: number
  completion_tokens: number
  cached_tokens: number
}

export interface AnalyticsRequest {
  id: string
  started_at: string
  model: string
  endpoint: string
  stream: boolean
  status: RequestStatus
  status_code: number | null
  duration_ms: number | null
  ttft_ms: number | null
  prompt_tokens: number
  completion_tokens: number
  reasoning_tokens: number
  cached_tokens: number
  total_tokens: number
  finish_reason: string | null
  usage_known: boolean
}

export interface AnalyticsResponse {
  demo: boolean
  range: AnalyticsRange
  granularity: 'hour' | 'day'
  period: { start: string; end: string }
  retention_days: number
  history_started_at: string | null
  totals: {
    requests: number
    success: number
    errors: number
    rate_limited: number
    cancelled: number
    prompt_tokens: number
    completion_tokens: number
    reasoning_tokens: number
    cached_tokens: number
    total_tokens: number
    avg_duration_ms: number | null
    avg_ttft_ms: number | null
    p95_duration_ms: number | null
    success_rate: number
    active_requests: number
    uptime_seconds: number
  }
  timeseries: AnalyticsBucket[]
  models: AnalyticsModel[]
  endpoints: AnalyticsDistribution[]
  requests: {
    items: AnalyticsRequest[]
    total: number
    page: number
    page_size: number
  }
  filters: { models: string[] }
}

export interface AnalyticsQuery {
  range: AnalyticsRange
  granularity: 'hour' | 'day'
  model: string
  status: RequestStatus | ''
  page: number
  page_size: number
  timezone_offset: number
}
