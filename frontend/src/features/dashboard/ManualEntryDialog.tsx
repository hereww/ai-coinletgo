import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, Check, Send, ShieldCheck, Sparkles, X } from 'lucide-react'
import { api } from '../../api/client'
import type { ManualEntryAdvice, ManualEntryDraft } from '../../api/types'
import { Button } from '../../components/Button'

interface Props {
  open: boolean
  onClose: () => void
}

type ChatMessage = { role: 'user' | 'assistant'; content: string }
type AdviceRequest = { draft: ManualEntryDraft; messages: ChatMessage[] }

const fallbackSymbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']

const initialDraft: ManualEntryDraft = {
  symbol: 'BTCUSDT',
  side: 'LONG',
  leverage: 2,
  stop_distance_pct: 1,
  tp1_r: 1,
  tp2_r: 2,
}

export function ManualEntryDialog({ open, onClose }: Props) {
  const queryClient = useQueryClient()
  const titleId = useId()
  const dialogRef = useRef<HTMLElement>(null)
  const previousFocusRef = useRef<HTMLElement | null>(null)
  const [draft, setDraft] = useState<ManualEntryDraft>(initialDraft)
  const [confirmed, setConfirmed] = useState(false)
  const [password, setPassword] = useState('')
  const [question, setQuestion] = useState('')
  const [messages, setMessages] = useState<ChatMessage[]>([
    { role: 'assistant', content: '告诉我你想交易的方向或顾虑。我会结合测试网行情检查参数，但不会替你直接下单。' },
  ])
  const config = useQuery({ queryKey: ['config'], queryFn: api.config, enabled: open })

  useEffect(() => {
    if (!open) return
    previousFocusRef.current = document.activeElement as HTMLElement | null
    setConfirmed(false)
    setPassword('')
    window.setTimeout(() => dialogRef.current?.focus(), 0)
    return () => previousFocusRef.current?.focus()
  }, [open])

  useEffect(() => {
    const configured = config.data?.entry_symbols
    if (!open || !configured?.length || configured.includes(draft.symbol)) return
    setDraft((current) => ({ ...current, symbol: configured[0] }))
    setConfirmed(false)
  }, [config.data, draft.symbol, open])

  useEffect(() => {
    const maximum = config.data?.max_leverage
    if (!open || !maximum || draft.leverage <= maximum) return
    setDraft((current) => ({ ...current, leverage: maximum }))
    setConfirmed(false)
  }, [config.data, draft.leverage, open])

  const entry = useMutation({
    mutationFn: () => api.manualEntry({ ...draft, operation_id: crypto.randomUUID(), password }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['dashboard'] }),
        queryClient.invalidateQueries({ queryKey: ['positions'] }),
        queryClient.invalidateQueries({ queryKey: ['orders'] }),
        queryClient.invalidateQueries({ queryKey: ['audit'] }),
      ])
    },
  })
  const advice = useMutation({
    mutationFn: ({ draft: requestDraft, messages: nextMessages }: AdviceRequest) => api.manualEntryAdvice({ ...requestDraft, messages: nextMessages }),
    onSuccess: (result) => setMessages((current) => [...current, { role: 'assistant', content: formatAdvice(result) }]),
  })

  if (!open) return null
  const symbols = config.data?.entry_symbols.length ? config.data.entry_symbols : fallbackSymbols
  const maxLeverage = Math.max(1, Math.min(30, config.data?.max_leverage ?? 3))
  const completed = Boolean(entry.data?.position?.protected)

  const update = <K extends keyof ManualEntryDraft>(key: K, value: ManualEntryDraft[K]) => {
    setDraft((current) => {
      const next = { ...current, [key]: value }
      if (key === 'tp1_r') next.tp2_r = Math.max(next.tp2_r, Number(value))
      if (key === 'tp2_r') next.tp2_r = Math.max(next.tp1_r, Number(value))
      return next
    })
    entry.reset()
    advice.reset()
    setConfirmed(false)
    setPassword('')
  }
  const ask = (event: FormEvent) => {
    event.preventDefault()
    const content = question.trim()
    if (!content || advice.isPending) return
    const requestDraft = draftForQuestion(draft, content, symbols)
    if (requestDraft.symbol !== draft.symbol) {
      setDraft(requestDraft)
      entry.reset()
      advice.reset()
      setConfirmed(false)
    }
    const next = [...messages, { role: 'user' as const, content }]
    setMessages(next)
    setQuestion('')
    advice.mutate({ draft: requestDraft, messages: next })
  }
  const applyAdvice = (result: ManualEntryAdvice) => {
    const suggestedSide = result.action === 'OPEN_LONG' ? 'LONG' : result.action === 'OPEN_SHORT' ? 'SHORT' : draft.side
    const suggestedTp1 = boundedNumber(result.tp1_r, 0.5, 10, draft.tp1_r)
    const suggestedTp2 = Math.max(suggestedTp1, boundedNumber(result.tp2_r, 2, 12, draft.tp2_r))
    setDraft((current) => ({
      ...current,
      side: suggestedSide,
      leverage: boundedNumber(result.leverage, 1, maxLeverage, current.leverage),
      stop_distance_pct: boundedNumber(result.stop_distance_pct, 0.1, 10, current.stop_distance_pct),
      tp1_r: suggestedTp1,
      tp2_r: suggestedTp2,
    }))
    entry.reset()
    setConfirmed(false)
  }
  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape' && !entry.isPending) onClose()
  }

  return (
    <div className="dialog-backdrop manual-entry-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && !entry.isPending && onClose()}>
      <section ref={dialogRef} className="manual-entry-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} onKeyDown={handleKeyDown} onMouseDown={(event) => event.stopPropagation()}>
        <header className="manual-entry-header">
          <div className="manual-entry-title">
            <span className="manual-entry-kicker">TESTNET ORDER DESK</span>
            <h2 id={titleId}>手动开仓</h2>
            <p>先设定风险，再提交订单。AI 建议区与下单区完全隔离。</p>
          </div>
          <button type="button" className="icon-button" aria-label="关闭手动开仓" onClick={onClose} disabled={entry.isPending}><X size={18} /></button>
        </header>

        <div className="manual-entry-columns">
          <div className="manual-order-panel">
            <div className="manual-panel-label"><ShieldCheck size={15} /><span>受保护订单</span><strong>仅测试网</strong></div>
            <div className="manual-entry-form-grid">
              <label className="field-label">合约
                <select value={draft.symbol} onChange={(event) => update('symbol', event.target.value)}>
                  {symbols.map((symbol) => <option key={symbol} value={symbol}>{symbol}</option>)}
                </select>
              </label>
              <label className="field-label">方向
                <select value={draft.side} onChange={(event) => update('side', event.target.value as ManualEntryDraft['side'])}>
                  <option value="LONG">做多 LONG</option>
                  <option value="SHORT">做空 SHORT</option>
                </select>
              </label>
              <label className="field-label">杠杆
                <select value={draft.leverage} onChange={(event) => update('leverage', Number(event.target.value))}>
                  {Array.from({ length: maxLeverage }, (_, index) => index + 1).map((value) => <option key={value} value={value}>{value}× 逐仓</option>)}
                </select>
              </label>
              <label className="field-label">止损距离 (%)
                <input type="number" min="0.1" max="10" step="0.1" value={draft.stop_distance_pct} onChange={(event) => update('stop_distance_pct', Number(event.target.value))} />
              </label>
              <label className="field-label">第一止盈 (R)
                <input type="number" min="0.5" max="10" step="0.5" value={draft.tp1_r} onChange={(event) => update('tp1_r', Number(event.target.value))} />
              </label>
              <label className="field-label">第二止盈 (R)
                <input type="number" min={Math.max(2, draft.tp1_r)} max="12" step="0.5" value={draft.tp2_r} onChange={(event) => update('tp2_r', Number(event.target.value))} />
              </label>
            </div>

            <div className="manual-risk-card">
              <div><span>仓位数量</span><strong>由硬风控计算</strong></div>
              <div><span>止损</span><strong>成交后立即挂出</strong></div>
              <div><span>止盈分配</span><strong>40% / 40% / 20%</strong></div>
            </div>
            <p className="manual-leverage-note">当前风控最高允许 {maxLeverage}×。提高杠杆不会提高系统允许的最大亏损。</p>

            {entry.error ? <div className="inline-error" role="alert">{entry.error.message}</div> : null}
            {entry.data ? (
              <div className={completed ? 'manual-entry-success' : 'success-note'}>
                <Check size={16} />
                <div><strong>{completed ? '仓位已成交并完成保护' : '订单执行完成'}</strong><span>{entry.data.entry.symbol} · 成交 {entry.data.entry.filled_quantity} · {entry.data.protection.length} 个保护单</span></div>
              </div>
            ) : null}

            {!entry.data ? (
              <>
                <label className="field-label manual-password-field">操作密码
                  <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" />
                </label>
                <label className="manual-confirm-row">
                  <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
                  <span>我确认这是测试网订单，并接受系统按硬风控自动计算数量。</span>
                </label>
              </>
            ) : null}
            <div className="manual-entry-actions">
              <Button type="button" onClick={onClose} disabled={entry.isPending}>{entry.data ? '完成' : '取消'}</Button>
              {!entry.data ? <Button type="button" variant="primary" icon={<ShieldCheck size={15} />} disabled={!confirmed || !password || entry.isPending} onClick={() => entry.mutate()}>{entry.isPending ? '提交并挂保护单...' : '确认测试网开仓'}</Button> : null}
            </div>
          </div>

          <aside className="manual-ai-panel">
            <div className="manual-ai-head"><div><Bot size={17} /><span>AI 开仓顾问</span></div><small>建议不自动执行</small></div>
            <div className="manual-chat-log" aria-live="polite">
              {messages.map((message, index) => (
                <div key={`${message.role}-${index}`} className={`manual-chat-message ${message.role}`}>
                  <span>{message.role === 'assistant' ? 'AI' : '你'}</span>
                  <p>{message.content}</p>
                </div>
              ))}
              {advice.isPending ? <div className="manual-chat-thinking"><Sparkles size={14} />正在读取测试网行情并评估参数…</div> : null}
              {advice.error ? <div className="inline-error">{advice.error.message}</div> : null}
            </div>
            {advice.data && advice.data.action !== 'NO_TRADE' ? (
              <div className="manual-advice-card">
                <div className="manual-advice-card-head"><div><Sparkles size={14} /><strong>可回填的结构化建议</strong></div><span>{advice.data.action === 'OPEN_LONG' ? '做多' : '做空'}</span></div>
                <div className="manual-advice-grid">
                  <div><small>合约</small><strong>{draft.symbol}</strong></div>
                  <div><small>杠杆</small><strong>{advice.data.leverage ?? '—'}×</strong></div>
                  <div><small>止损距离</small><strong>{advice.data.stop_distance_pct ?? '—'}%</strong></div>
                  <div><small>第一止盈</small><strong>{advice.data.tp1_r ?? '—'}R</strong></div>
                  <div><small>第二止盈</small><strong>{advice.data.tp2_r ?? '—'}R</strong></div>
                </div>
                <button type="button" className="manual-apply-advice" onClick={() => applyAdvice(advice.data)}><Check size={14} />一键填写到左侧开仓参数</button>
              </div>
            ) : null}
            <form className="manual-chat-compose" onSubmit={ask}>
              <textarea aria-label="向 AI 询问开仓建议" value={question} onChange={(event) => setQuestion(event.target.value)} placeholder={`例如：分析 ${draft.symbol} 现在做${draft.side === 'LONG' ? '多' : '空'}是否合适？`} rows={3} maxLength={1000} />
              <Button type="submit" variant="primary" icon={<Send size={14} />} disabled={!question.trim() || advice.isPending}>发送</Button>
            </form>
          </aside>
        </div>
      </section>
    </div>
  )
}

function formatAdvice(result: ManualEntryAdvice) {
  const action = result.action === 'OPEN_LONG' ? '建议做多' : result.action === 'OPEN_SHORT' ? '建议做空' : '建议暂不开仓'
  const confidence = `${(Number(result.confidence) * 100).toFixed(0)}%`
  const parameters = result.action === 'NO_TRADE' ? '' : `\n参数：${result.leverage ?? '—'}×，止损 ${result.stop_distance_pct ?? '—'}%，TP1 ${result.tp1_r ?? '—'}R，TP2 ${result.tp2_r ?? '—'}R。`
  const risks = result.risk_notes.length ? `\n风险：${result.risk_notes.join('；')}` : ''
  return `${action} · 置信度 ${confidence}\n${result.reply}${parameters}${risks}`
}

function draftForQuestion(current: ManualEntryDraft, question: string, symbols: string[]) {
  const normalized = question.toUpperCase()
  const requested = [...symbols]
    .sort((left, right) => right.length - left.length)
    .find((symbol) => normalized.includes(symbol) || normalized.includes(symbol.replace(/USDT$/, '')))
  return requested && requested !== current.symbol ? { ...current, symbol: requested } : current
}

function boundedNumber(value: number | string | null, minimum: number, maximum: number, fallback: number) {
  const parsed = value === null ? Number.NaN : Number(value)
  return Number.isFinite(parsed) ? Math.min(maximum, Math.max(minimum, parsed)) : fallback
}
