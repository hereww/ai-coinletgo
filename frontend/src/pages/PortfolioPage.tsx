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
  const cycle = useQuery({
    queryKey: ['cycle-status'],
    queryFn: api.cycleStatus,
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
    {cycle.data ? <div className={`cycle-status cycle-status-${cycle.data.state.toLowerCase()}`} role="status"><strong>{cycleStateLabel[cycle.data.state] ?? cycle.data.state}</strong><span>{cycle.data.detail}</span><small>快照 {cycle.data.snapshots} · 候选 {cycle.data.candidates} · 模型输出 {cycle.data.signals} · 执行 {cycle.data.executed}</small></div> : null}
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
            <td>{decision.payload.allocations.length} 个</td>
            <td><button className="table-expand-button" type="button" onClick={() => setExpanded(isOpen ? null : decision.id)}><ChevronDown size={14} className={isOpen ? 'rotated' : ''} />{isOpen ? '收起' : '展开'}</button></td>
          </tr>
          {isOpen ? <tr key={`${decision.id}-details`} className="signal-detail-table-row"><td colSpan={7}><div className="portfolio-detail"><p>{payload.summary}</p><div className="portfolio-target-strip">{payload.allocations.map((allocation) => <span key={allocation.allocation_id}><strong>{allocation.symbol}</strong> {allocation.target_side} · {(Number(allocation.allocation_fraction) * 100).toFixed(0)}%</span>)}</div><div className="portfolio-allocation-grid">{decision.allocations.map((allocation) => {
            const action = allocation.action ?? 'AI_TARGET'
            const reasons = allocation.reasons?.length
              ? allocation.reasons
              : [...(allocation.reason_codes ?? []), ...(allocation.risk_flags ?? [])]
            const hasCompiledAction = Boolean(allocation.action)
            return <div className="portfolio-allocation" key={allocation.action_id}><div><strong>{allocation.symbol}</strong><span className={`result-text ${action.toLowerCase()}`}>{hasCompiledAction ? action : 'AI目标'}</span></div><dl><div><dt>方向</dt><dd>{allocation.side ?? allocation.target_side ?? '—'}</dd></div><div><dt>当前 → 目标</dt><dd>{allocation.current_quantity != null ? `${allocation.current_quantity} → ${allocation.target_quantity ?? '—'}` : '尚未编译'}</dd></div><div><dt>风险</dt><dd>{allocation.target_risk_usdt != null ? `${allocation.target_risk_usdt} USDT` : `${((Number(allocation.allocation_fraction ?? 0)) * 100).toFixed(0)}%（待编译）`}</dd></div><div><dt>原因</dt><dd>{reasons.join('、') || allocation.thesis || (hasCompiledAction ? '—' : 'AI 意图已记录，等待确定性风控结果')}</dd></div></dl>{allocation.execution ? <small>执行：{allocation.execution.status} · {allocation.execution.order_ids.join(', ') || '无订单'}</small> : <small>{hasCompiledAction ? '尚无执行记录' : '本轮未生成确定性调仓动作'}</small>}</div>
          })}</div><small>失效时间：{payload.expires_at} · 提示词：{decision.prompt_version}</small></div></td></tr> : null}
        </Fragment>
      })}</tbody></table></div>}
    </section>
  </>
}

const cycleStateLabel: Record<string, string> = {
  RUNNING: '本轮执行中',
  COMPLETED: '本轮已完成',
  EXECUTED: '本轮已有执行',
  NO_CANDIDATES: '本轮无候选，未调用模型',
  RISK_REJECTED: '模型已返回，但被硬风控拒绝',
  MODEL_UNAVAILABLE: '模型不可用，本轮未增险',
  MODEL_TIMEOUT: '模型响应超时，本轮未增险',
  MODEL_THROTTLED: '模型节流中，本轮未重复调用',
  BLOCKED_RECONCILIATION: '需先完成仓位接管，本轮未调用模型',
  EXCHANGE_UNAVAILABLE: '交易所暂不可用，本轮未调用模型',
  WORKER_BUSY: '上一轮仍在执行，本轮未重复启动',
  FAILED: '本轮异常，已安全停止',
  UNKNOWN: '等待 Worker 状态',
}
