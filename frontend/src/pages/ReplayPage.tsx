import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Play } from 'lucide-react'
import { api } from '../api/client'
import { Button } from '../components/Button'
import { PageHeader } from '../components/PageHeader'
import { formatTime } from '../features/signals/signalPresentation'

const beijingDate = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})

type ReplayMode = 'deterministic' | 'recorded_portfolio'

export default function ReplayPage() {
  const queryClient = useQueryClient()
  const now = new Date()
  const [mode, setMode] = useState<ReplayMode>('deterministic')
  const [symbols, setSymbols] = useState('BTCUSDT,ETHUSDT')
  const [startDate, setStartDate] = useState(
    beijingDate.format(new Date(now.getTime() - 30 * 86_400_000)),
  )
  const [endDate, setEndDate] = useState(beijingDate.format(now))
  const [decisionId, setDecisionId] = useState('')
  const config = useQuery({ queryKey: ['config'], queryFn: api.config })
  const runs = useQuery({ queryKey: ['replays'], queryFn: api.replays, refetchInterval: 5_000 })
  const decisions = useQuery({ queryKey: ['portfolio-decisions'], queryFn: api.portfolioDecisions })
  const replay = useMutation({
    mutationFn: api.createReplay,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['replays'] }),
  })
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (mode === 'recorded_portfolio') {
      replay.mutate({ mode, portfolio_decision_id: decisionId })
      return
    }
    replay.mutate({
      mode,
      symbols: symbols.split(',').map((item) => item.trim().toUpperCase()).filter(Boolean),
      start_date: startDate,
      end_date: endDate,
      backtest_config: config.data ? {
        risk_pct: String(config.data.single_trade_risk_pct),
        portfolio_risk_pct: String(config.data.portfolio_risk_pct),
        max_leverage: config.data.max_leverage,
        max_margin_pct: String(config.data.max_margin_pct),
        max_positions: config.data.max_positions,
        max_same_direction: config.data.max_same_direction,
        correlation_limit: String(config.data.correlation_limit),
        candidate_count: config.data.candidate_count,
        entry_direction: config.data.entry_direction,
        entry_trigger: config.data.entry_trigger,
        min_confidence: String(config.data.min_confidence),
        min_net_reward_risk: String(config.data.min_net_reward_risk),
        min_stop_atr: String(config.data.min_stop_atr),
        max_stop_atr: String(config.data.max_stop_atr),
        trend_adx_min: String(config.data.trend_adx_min ?? 20),
        volatility_soft_limit_percentile: String(config.data.volatility_soft_limit_percentile ?? 0.75),
        volatility_hard_limit_percentile: String(config.data.volatility_hard_limit_percentile ?? 0.9),
        elevated_volatility_risk_multiplier: String(config.data.elevated_volatility_risk_multiplier ?? 0.75),
        high_volatility_risk_multiplier: String(config.data.high_volatility_risk_multiplier ?? 0.5),
      } : undefined,
    })
  }
  const latest = runs.data?.[0]
  const summary = latest?.metrics.summary as Record<string, string | number | boolean> | undefined
  const isRecorded = latest?.parameters.mode === 'recorded_portfolio'
  const netReturn = summary?.portfolio_net_return_pct ?? summary?.average_net_return_pct
  const maxDrawdown = summary?.max_drawdown_pct ?? summary?.worst_max_drawdown_pct
  const validation = latest?.metrics.validation as { out_of_sample?: { summary?: Record<string, string | number | boolean> } } | undefined
  const outOfSample = validation?.out_of_sample?.summary

  return <>
    <PageHeader title="历史回放" subtitle="可重复的本地策略回放与 Portfolio-v1 决策复现 · 北京时间" />
    <section className="surface replay-layout">
      <form className="replay-form" onSubmit={submit}>
        <h2>创建回放任务</h2>
        <label className="field-label">回放模式
          <select value={mode} onChange={(event) => setMode(event.target.value as ReplayMode)}>
            <option value="deterministic">确定性策略回放</option>
            <option value="recorded_portfolio">已保存组合决策复现</option>
          </select>
        </label>
        {mode === 'deterministic' ? <>
          <p className="form-help">使用本地固定策略与历史行情；不会调用远端模型。</p>
          <label className="field-label">合约列表<input value={symbols} onChange={(event) => setSymbols(event.target.value)} required /></label>
          <div className="form-grid">
            <label className="field-label">开始日期<input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} required /></label>
            <label className="field-label">结束日期<input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} required /></label>
          </div>
        </> : <>
          <p className="form-help">使用已保存的决策、市场快照、账户状态、交易所规则和编译器时钟，验证调仓计划是否可重复。</p>
          <label className="field-label">组合决策
            <select value={decisionId} onChange={(event) => setDecisionId(event.target.value)} required>
              <option value="" disabled>选择一条已保存的组合决策</option>
              {(decisions.data ?? []).map((decision) => <option key={decision.id} value={decision.id}>
                {formatTime(decision.created_at, { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })} · {decision.market_regime} · {decision.id.slice(0, 8)}
              </option>)}
            </select>
          </label>
          {!decisions.isLoading && !(decisions.data ?? []).length ? <p className="inline-error">暂无可复现的组合决策。需要先由 Portfolio-v1 保存一轮决策。</p> : null}
        </>}
        {replay.error ? <div className="inline-error">{replay.error.message}</div> : null}
        <Button type="submit" variant="primary" icon={<Play size={15} />} disabled={replay.isPending || (mode === 'recorded_portfolio' && !decisionId)}>
          {replay.isPending ? '正在创建...' : '开始回放'}
        </Button>
      </form>
      <div className="replay-summary">
        <h2>最近任务</h2>
        <dl>
          <div><dt>状态</dt><dd>{latest?.status ?? '—'}</dd></div>
          <div><dt>模式</dt><dd>{isRecorded ? '决策复现' : latest ? '确定性策略' : '—'}</dd></div>
          {isRecorded ? <>
            <div><dt>编译计划</dt><dd>{summary?.compiler_plan_match === true ? '一致' : summary?.compiler_plan_match === false ? '不一致' : '—'}</dd></div>
            <div><dt>计划动作</dt><dd>{summary?.planned_actions ?? '—'}</dd></div>
          </> : <>
            <div><dt>组合净收益</dt><dd>{netReturn !== undefined ? `${(Number(netReturn) * 100).toFixed(2)}%` : '—'}</dd></div>
            <div><dt>组合最大回撤</dt><dd>{maxDrawdown !== undefined ? `${(Number(maxDrawdown) * 100).toFixed(2)}%` : '—'}</dd></div>
            <div><dt>交易数</dt><dd>{summary?.total_trades ?? '—'}</dd></div>
            <div><dt>合约数</dt><dd>{summary?.symbols_tested ?? '—'}</dd></div>
            <div><dt>样本外胜率</dt><dd>{outOfSample?.win_rate !== undefined ? `${(Number(outOfSample.win_rate) * 100).toFixed(1)}%` : '—'}</dd></div>
            <div><dt>样本外交易数</dt><dd>{outOfSample?.total_trades ?? '—'}</dd></div>
          </>}
        </dl>
        {replay.data ? <div className="success-note">任务 {replay.data.id} 已进入队列</div> : null}
      </div>
    </section>
    <section className="surface page-surface">
      <div className="section-head"><h2>任务记录</h2><span>{runs.data?.length ?? 0} 条</span></div>
      <div className="table-scroll"><table className="data-table"><thead><tr><th>创建时间</th><th>模式</th><th>范围</th><th>状态</th><th>可重复性</th></tr></thead><tbody>
        {(runs.data ?? []).map((run) => {
          const recorded = run.parameters.mode === 'recorded_portfolio'
          const runSummary = run.metrics.summary as Record<string, string | number | boolean> | undefined
          return <tr key={run.id}>
            <td>{formatTime(run.created_at, { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })}</td>
            <td>{recorded ? '决策复现' : '确定性策略'}</td>
            <td>{recorded ? `决策 ${run.parameters.portfolio_decision_id?.slice(0, 8) ?? '—'}` : `${run.parameters.symbols?.join(', ') ?? '—'} · ${run.parameters.start_date ?? '—'} 至 ${run.parameters.end_date ?? '—'}`}</td>
            <td><span className={`result-text ${run.status.toLowerCase()}`}>{run.status}</span></td>
            <td>{recorded ? runSummary?.compiler_plan_match === true ? '计划一致' : runSummary?.compiler_plan_match === false ? '计划不一致' : '待验证' : '本地固定策略'}</td>
          </tr>
        })}
      </tbody></table></div>
    </section>
  </>
}
