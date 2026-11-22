export const number = (value: number) => value.toLocaleString('zh-CN')

export function compact(value: number): string {
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)}B`
  if (value >= 1e6) return `${(value / 1e6).toFixed(2)}M`
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}K`
  return String(Math.round(value))
}

export function duration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return '—'
  return `${(ms / 1000).toFixed(2)} s`
}

export function clock(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds))
  const days = Math.floor(value / 86400)
  const time = [
    Math.floor(value / 3600) % 24,
    Math.floor(value / 60) % 60,
    value % 60,
  ]
    .map((v) => String(v).padStart(2, '0'))
    .join(':')
  return days ? `${days}天 ${time}` : time
}
