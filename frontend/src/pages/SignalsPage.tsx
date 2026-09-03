import { Fragment, useDeferredValue, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, Search } from 'lucide-react'
import { api } from '../api/client'
import { EmptyState } from '../components/EmptyState'
import { PageHeader } from '../components/PageHeader'
import { PageError, PageLoading } from '../components/PageState'
import { CycleStatusBanner } from '../features/cycle/CycleStatusBanner'
import { PortfolioDecisionsList } from '../features/dashboard/PortfolioDecisionsList'
import { SignalDecisionDetails } from '../features/signals/SignalDecisionDetails'
import { actionLabel, displayAdvice, displayReason, formatTime, resultClass, resultLabel } from '../features/signals/signalPresentation'

export default function SignalsPage() {
  const [query, setQuery] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const deferredQuery = useDeferredValue(query.trim().toUpperCase())
  const signals = useQuery({ queryKey: ['signals'], queryFn: api.signals, refetchInterval: 15_000 })
  const portfolio = useQuery({ queryKey: ['portfolio-decisions'], queryFn: api.portfolioDecisions, refetchInterval: 15_000 })
  const cycle = useQuery({ queryKey: ['cycle-status'], queryFn: api.cycleStatus, refetchInterval: 15_000 })
  if (signals.isLoading || portfolio.isLoading || cycle.isLoading) return <><PageHeader title="模型信号" subtitle="结构化输出与硬风控结果" /><PageLoading /></>
  if (signals.isError || portfolio.isError || cycle.isError) {
    const error = signals.error ?? portfolio.error ?? cycle.error
    return <><PageHeader title="模型信号" subtitle="结构化输出与硬风控结果" /><PageError message={error?.message ?? '决策数据不可用'} retry={() => { void signals.refetch(); void portfolio.refetch(); void cycle.refetch() }} /></>
  }
  const rows = (signals.data ?? []).filter((signal) => !deferredQuery || signal.symbol.includes(deferredQuery))
  const portfolioRows = portfolio.data ?? []
  return (
    <>
      <PageHeader title="模型信号" subtitle="结构化输出与硬风控结果" actions={<label className="search-field"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选合约" /></label>} />
      {cycle.data ? <CycleStatusBanner status={cycle.data} /> : null}
      <section className="surface page-surface current-strategy-surface">
        <div className="section-head"><h2>当前 Portfolio-v1 决策</h2><span>{portfolioRows.length ? `最新 ${formatTime(portfolioRows[0].created_at)}` : '等待新决策'}</span></div>
        <PortfolioDecisionsList decisions={portfolioRows} cycleStatus={cycle.data!} />
      </section>
      <section className="surface page-surface legacy-signals-surface">
        <div className="section-head"><h2>兼容历史逐币信号</h2><span>{rows.length} 条 · 旧版记录</span></div>
        <p className="setup-note">此处仅保留 Portfolio-v1 启用前的旧版逐币信号；当前周期请以上方 Portfolio-v1 决策为准。时间统一按北京时间（UTC+8）显示。</p>
        {!rows.length ? <EmptyState title="没有匹配信号" detail="模型输出、中文原因和建议会按时间保留。" /> : <div className="table-scroll"><table className="data-table"><thead><tr><th>时间（北京时间）</th><th>合约</th><th>动作</th><th>置信度</th><th>结果</th><th>原因</th><th>AI建议</th><th>详情</th></tr></thead><tbody>{rows.map((signal) => {
          const expanded = expandedId === signal.id
          return <Fragment key={signal.id}>
            <tr>
              <td>{formatTime(signal.created_at)}</td>
              <td><strong>{signal.symbol}</strong></td>
              <td>{actionLabel(signal.action)}</td>
              <td className="mono">{(Number(signal.confidence) * 100).toFixed(0)}%</td>
              <td><span className={`result-text ${resultClass(signal.result)}`}>{resultLabel(signal.result)}</span></td>
              <td className="reason-cell"><span className="reason-title">原因</span>{displayReason(signal)}</td>
              <td className="advice-cell"><span className="advice-title">建议</span>{displayAdvice(signal)}</td>
              <td><button className="table-expand-button" type="button" aria-expanded={expanded} aria-controls={`signal-table-details-${signal.id}`} onClick={() => setExpandedId(expanded ? null : signal.id)}><ChevronDown size={14} className={expanded ? 'rotated' : ''} />{expanded ? '收起' : '展开'}</button></td>
            </tr>
            {expanded ? <tr className="signal-detail-table-row"><td colSpan={8} id={`signal-table-details-${signal.id}`}><SignalDecisionDetails signal={signal} /></td></tr> : null}
          </Fragment>
        })}</tbody></table></div>}
      </section>
    </>
  )
}
