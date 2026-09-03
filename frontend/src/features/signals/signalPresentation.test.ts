import { describe, expect, it } from 'vitest'
import { formatTime, parseApiDate } from './signalPresentation'

describe('formatTime', () => {
  it('always displays API timestamps in Beijing time', () => {
    expect(formatTime('2026-08-28T02:45:00Z')).toBe('08/28 10:45')
    expect(formatTime('2026-08-28T10:45:00+08:00')).toBe('08/28 10:45')
  })

  it('treats legacy timestamps without an offset as UTC', () => {
    expect(formatTime('2026-08-28T02:45:00')).toBe('08/28 10:45')
  })

  it('uses the same timestamp interpretation for ordering', () => {
    expect(parseApiDate('2026-08-28T02:45:00').getTime()).toBe(parseApiDate('2026-08-28T10:45:00+08:00').getTime())
  })
})
