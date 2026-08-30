import { Fragment, useState } from 'react'
import { ChevronDown, Layers3 } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { EmptyState } from '../components/EmptyState'
import { PageHeader } from '../components/PageHeader'
import { PageError, PageLoading } from '../components/PageState'
import type { PortfolioDecision } from '../api/types'

const regimeLabel: Record<PortfolioDecision['market_regime'], string> = {
  TRENDING: '趋势',
  RANGING: '震荡',
  VOLATILE: '高波动',
  UNCERTAIN: '不确定',
}

const statusLabel: Record<string, string> = {
  APPROVED: '已批准',
  PARTIALLY_APPROVED: '部分批准',
  NO_ACTION: '无调仓',
  REJECTED: '已拒绝',
  EXECUTED: '已执行',
  FAILED: '执行失败',
}

export default function PortfolioPage() {
  const decisions = useQuery({
    queryKey: ['portfolio-decisions'],
    queryFn: api.portfolioDecisions,
    refetchInterval: 15_000,
  })
  const [expanded, setExpanded] = useState<string | null>(null)

  if (decisions.isLoading) {
    return <><PageHeader title="组合决策" subtitle="AI 目标、风险预算与实际调仓计划" /><PageLoading /></>
  }
  if (decisions.isError) {
    return <><PageHeader title="组合决策" subtitle="AI 目标、风险预算与实际调仓计划" /><PageError message={decisions.error.message} retry={() => decisions.refetch()} /></>
  }
  const rows = decisions.data ?? []
  return <>
    <PageHeader title="组合决策" subtitle="AI 目标、风险预算与实际调仓计划" />
    <section className="surface page-surface portfolio-page">
      <div className="section-head"><h2><Layers3 size={16} /> 最近组合决策</h2><span>{rows.length} 条</span></div>
      {!rows.length ? <EmptyState title="还没有组合决策" detail="开启 Portfolio-v1 后，模型的组合意图和确定性裁剪结果会显示在这里。" /> : <div className="table-scroll"><table className="data-table"><thead><tr><th>时间</th><th>状态</th><th>市场状态</th><th>风险预算</th><th>模型</th><th>分配</th><th>详情</th></tr></thead><tbody>{rows.map((decision) => {
        const isOpen = expanded === decision.id
        const payload = decision.payload
        return <Fragment key={decision.id}>
          <tr key={decision.id}>
            <td>{new Date(decision.created_at).toLocaleString('zh-CN')}</td>
            <td><span className={`result-text ${decision.status.toLowerCase()}`}>{statusLabel[decision.status] ?? decision.status}</span></td>
            <td>{regimeLabel[decision.market_regime]}</td>
            <td className="mono">{(Number(decision.portfolio_risk_budget_fraction) * 100).toFixed(0)}%</td>
            <td>{decision.model_name}</td>
            <td>{decision.allocations.length} 个</td>
            <td><button className="table-expand-button" type="button" onClick={() => setExpanded(isOpen ? null : decision.id)}><ChevronDown size={14} className={isOpen ? 'rotated' : ''} />{isOpen ? '收起' : '展开'}</button></td>
          </tr>
          {isOpen ? <tr key={`${decision.id}-details`} className="signal-detail-table-row"><td colSpan={7}><div className="portfolio-detail"><p>{payload.summary}</p><div className="portfolio-target-strip">{payload.allocations.map((allocation) => <span key={allocation.allocation_id}><strong>{allocation.symbol}</strong> {allocation.target_side} · {(Number(allocation.allocation_fraction) * 100).toFixed(0)}%</span>)}</div><div className="portfolio-allocation-grid">{decision.allocations.map((allocation) => <div className="portfolio-allocation" key={allocation.action_id}><div><strong>{allocation.symbol}</strong><span className={`result-text ${allocation.action.toLowerCase()}`}>{allocation.action}</span></div><dl><div><dt>方向</dt><dd>{allocation.side ?? '—'}</dd></div><div><dt>当前 → 目标</dt><dd>{allocation.current_quantity} → {allocation.target_quantity}</dd></div><div><dt>风险</dt><dd>{allocation.target_risk_usdt} USDT</dd></div><div><dt>原因</dt><dd>{allocation.reasons.join('、') || '—'}</dd></div></dl>{allocation.execution ? <small>执行：{allocation.execution.status} · {allocation.execution.order_ids.join(', ') || '无订单'}</small> : null}</div>)}</div><small>失效时间：{payload.expires_at} · 提示词：{decision.prompt_version}</small></div></td></tr> : null}
        </Fragment>
      })}</tbody></table></div>}
    </section>
  </>
}
