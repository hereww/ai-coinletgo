import { useState } from 'react'
import { ChevronDown, CheckCircle2, MinusCircle, ShieldX } from 'lucide-react'
import type { Signal } from '../../api/types'
import { SignalDecisionDetails } from '../signals/SignalDecisionDetails'
import { actionLabel, displayAdvice, displayReason, formatTime, resultClass, resultLabel } from '../signals/signalPresentation'

export function SignalsList({ signals }: { signals: Signal[] }) {
  const [expandedId, setExpandedId] = useState<string | null>(null)

  if (!signals.length) {
    return <div className="compact-empty">暂无模型决策</div>
  }
  return (
    <div className="signal-list">
      {signals.map((signal) => {
        const Icon = signal.result === 'APPROVED' ? CheckCircle2 : signal.result.startsWith('REJECTED') ? ShieldX : MinusCircle
        const expanded = expandedId === signal.id
        return (
          <article className={`signal-entry ${expanded ? 'expanded' : ''}`} key={signal.id}>
            <button
              className="signal-row"
              type="button"
              aria-expanded={expanded}
              aria-controls={`signal-details-${signal.id}`}
              onClick={() => setExpandedId(expanded ? null : signal.id)}
            >
              <Icon size={17} className={resultClass(signal.result)} aria-hidden="true" />
              <span className="signal-main"><span className="signal-title"><strong>{signal.symbol}</strong><span>{actionLabel(signal.action)} · {resultLabel(signal.result)}</span></span><span className="signal-reason">{displayReason(signal)}</span><span className="signal-advice">{displayAdvice(signal)}</span></span>
              <span className="signal-meta"><strong>{(Number(signal.confidence) * 100).toFixed(0)}%</strong><time>{formatTime(signal.created_at, { hour: '2-digit', minute: '2-digit' })}</time><ChevronDown size={15} className="signal-chevron" aria-hidden="true" /></span>
            </button>
            {expanded ? <div className="signal-detail-wrap" id={`signal-details-${signal.id}`}><SignalDecisionDetails signal={signal} /></div> : null}
          </article>
        )
      })}
    </div>
  )
}
