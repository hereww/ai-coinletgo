import type { Signal } from '../../api/types'

export const APP_TIME_ZONE = 'Asia/Shanghai'

export const actionLabel = (action: Signal['action']) => ({
  OPEN_LONG: '开多',
  OPEN_SHORT: '开空',
  NO_TRADE: '不交易',
})[action]

export const resultLabel = (result: Signal['result']) => ({
  APPROVED: '已批准',
  REJECTED: '已拒绝',
  NO_TRADE: '不交易',
  REJECTED_UNKNOWN_SYMBOL: '已拒绝',
}[result] ?? (result.startsWith('REJECTED') ? '已拒绝' : result))

export const resultClass = (result: Signal['result']) => {
  if (result === 'APPROVED') return 'approved'
  if (result === 'NO_TRADE') return 'no_trade'
  return 'rejected'
}

export const displayReason = (signal: Signal) => signal.reason_zh ?? signal.reason ?? '本轮未开仓，中文原因暂未返回。'

export const displayAdvice = (signal: Signal) => signal.recommendation_zh ?? signal.ai_advice ?? '建议等待下一轮周期，确认趋势、触发和风险条件后再评估。'

export const parseApiDate = (value: string) => {
  // SQLite can return timezone-aware columns without an offset. Persisted API
  // timestamps are UTC, so make that assumption explicit before parsing.
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`
  return new Date(normalized)
}

export const formatTime = (value: string, options: Intl.DateTimeFormatOptions = { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) =>
  new Intl.DateTimeFormat('zh-CN', { ...options, timeZone: APP_TIME_ZONE }).format(parseApiDate(value))

export const formatNumber = (value: string | number | null | undefined, maximumFractionDigits = 6) => {
  if (value == null || value === '') return '—'
  const number = Number(value)
  return Number.isFinite(number)
    ? number.toLocaleString('zh-CN', { maximumFractionDigits })
    : '—'
}

export const formatPercent = (value: string | number | null | undefined, maximumFractionDigits = 3) => {
  if (value == null || value === '') return '—'
  const number = Number(value)
  return Number.isFinite(number) ? `${(number * 100).toFixed(maximumFractionDigits)}%` : '—'
}

export const trendLabel = (value: -1 | 0 | 1) => value === 1 ? '多头' : value === -1 ? '空头' : '中性'

export const triggerLabel = (value: -1 | 0 | 1, kind: 'breakout' | 'pullback') => {
  if (value === 0) return '未触发'
  const direction = value === 1 ? '向上' : '向下'
  return `${direction}${kind === 'breakout' ? '突破' : '回踩'}`
}
