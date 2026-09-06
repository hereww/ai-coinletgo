import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Database, FlaskConical, LoaderCircle } from 'lucide-react'
import { api } from '../api/client'
import type {
  FactorPortfolioMetrics,
  FactorResearchRequest,
  FactorResearchResult,
  FactorResearchRun,
} from '../api/types'
import { Button } from '../components/Button'
import { PageHeader } from '../components/PageHeader'

const beijingDate = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})

const statusLabels: Record<FactorResearchResult['factors'][number]['status'], string> = {
  PASSED: '通过',
  WATCH: '观察',
  INSUFFICIENT: '样本不足',
  UNAVAILABLE: '数据缺失',
}

function formatMetric(value: number | null, digits = 3) {
  if (value === null || !Number.isFinite(value)) return '—'
  return value.toFixed(digits)
}

function formatPercent(value: number | null) {
  if (value === null || !Number.isFinite(value)) return '—'
  return `${(value * 100).toFixed(1)}%`
}

function latestDecay(factor: FactorResearchResult['factors'][number]) {
  const point = factor.decay.at(-1)
  return point ? formatMetric(point.mean_ic) : '—'
}

function portfolioMetric(
  portfolio: FactorPortfolioMetrics | null | undefined,
  key: keyof FactorPortfolioMetrics,
) {
  if (!portfolio) return null
  const value = portfolio[key]
  return typeof value === 'number' ? value : null
}

function preferredPortfolio(factor: FactorResearchResult['factors'][number]) {
  return factor.walk_forward_portfolio ?? factor.portfolio
}

function completedReport(run: FactorResearchRun | undefined): FactorResearchResult | null {
  if (run?.status !== 'COMPLETED' || !('factors' in run.report)) return null
  return run.report
}

function failedRunMessage(run: FactorResearchRun | undefined) {
  if (run?.status !== 'FAILED' || !('error' in run.report)) return null
  return run.report.error ?? '因子研究任务失败'
}

const runStatusLabels: Record<FactorResearchRun['status'], string> = {
  QUEUED: '任务已排队',
  RUNNING: '正在获取公共行情并计算',
  COMPLETED: '研究完成',
  FAILED: '研究失败',
}

export default function FactorResearchPage() {
  const queryClient = useQueryClient()
  const now = new Date()
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [symbols, setSymbols] = useState(
    'BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT',
  )
  const [startDate, setStartDate] = useState(
    beijingDate.format(new Date(now.getTime() - 90 * 86_400_000)),
  )
  const [endDate, setEndDate] = useState(beijingDate.format(now))
  const [interval, setInterval] = useState<'1h' | '4h'>('1h')
  const [forwardBars, setForwardBars] = useState(24)
  const [rebalanceBars, setRebalanceBars] = useState(24)
  const catalog = useQuery({ queryKey: ['factor-catalog'], queryFn: api.factorCatalog })
  const runs = useQuery({
    queryKey: ['factor-research-runs'],
    queryFn: api.factorResearchRuns,
    refetchInterval: (query) => query.state.data?.some(
      (run) => run.status === 'QUEUED' || run.status === 'RUNNING',
    ) ? 2_500 : false,
  })
  const shadow = useQuery({
    queryKey: ['factor-shadow-rankings'],
    queryFn: api.factorShadowRankings,
    refetchInterval: 10_000,
  })
  const policy = useQuery({
    queryKey: ['factor-policy'],
    queryFn: api.factorPolicyStatus,
    refetchInterval: 10_000,
  })
  const research = useMutation({
    mutationFn: api.researchFactors,
    onSuccess: (submission) => {
      setActiveRunId(submission.id)
      void queryClient.invalidateQueries({ queryKey: ['factor-research-runs'] })
    },
  })

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const normalized = symbols
      .split(',')
      .map((item) => item.trim().toUpperCase())
      .filter((item, index, values) => item && values.indexOf(item) === index)
    const payload: FactorResearchRequest = {
      symbols: normalized,
      start_date: startDate,
      end_date: endDate,
      interval,
      forward_bars: forwardBars,
      rebalance_bars: rebalanceBars,
      winsorize_quantile: '0.05',
      min_cross_section: 3,
      maker_fee_rate: '0.0002',
      taker_fee_rate: '0.0005',
      slippage_rate: '0.0005',
      funding_rate_fallback: '0.0001',
      walk_forward_folds: 4,
      portfolio_quantile: '0.2',
    }
    research.mutate(payload)
  }

  const activeRun = activeRunId
    ? runs.data?.find((run) => run.id === activeRunId)
    : runs.data?.[0]
  const result = completedReport(activeRun)
    ?? runs.data?.map(completedReport).find((report) => report !== null)
    ?? null
  const awaitingActiveRun = activeRunId !== null && activeRun === undefined
  const runPending = awaitingActiveRun || activeRun?.status === 'QUEUED' || activeRun?.status === 'RUNNING'
  const runError = failedRunMessage(activeRun)
  const sources = result?.data_sources ?? catalog.data?.data_sources ?? []

  return <div className="factor-page">
    <PageHeader
      title="因子研究"
      subtitle="横截面研究、在线影子窗口与测试网升版状态"
    />

    {policy.data ? <section className="surface factor-policy-status">
      <div className="section-head">
        <h2>策略版本</h2>
        <span>{policy.data.enabled ? '测试网因子执行已启用' : '因子执行已关闭'}</span>
      </div>
      <div className="factor-metric-strip" aria-label="因子升版进度">
        <div><span>ACTIVE</span><strong>{policy.data.active?.research_run_id.slice(0, 8) ?? '—'}</strong></div>
        <div><span>SHADOW</span><strong>{policy.data.shadow?.research_run_id.slice(0, 8) ?? '—'}</strong></div>
        <div><span>成熟窗口</span><strong>{policy.data.promotion.matured_windows}/{policy.data.promotion.required_windows}</strong></div>
        <div><span>方向调整 IC</span><strong>{formatMetric(policy.data.promotion.oriented_mean_ic === null ? null : Number(policy.data.promotion.oriented_mean_ic))}</strong></div>
        <div><span>影子净收益</span><strong>{formatPercent(Number(policy.data.promotion.shadow_net_return))}</strong></div>
        <div><span>升版资格</span><strong className={policy.data.promotion.eligible_for_promotion ? 'positive' : 'warning'}>{policy.data.promotion.eligible_for_promotion ? '通过' : '未通过'}</strong></div>
      </div>
      <div className="factor-policy-progress">
        <progress value={Number(policy.data.promotion.progress)} max={1} />
        <span>{policy.data.promotion.failure_reasons.length ? policy.data.promotion.failure_reasons.join(' · ') : '全部升版门槛已通过，下一交易周期切换'}</span>
      </div>
    </section> : null}

    <section className="surface factor-workbench">
      <form className="factor-form" onSubmit={submit}>
        <div className="section-head factor-section-head">
          <h2><FlaskConical size={15} /> 研究参数</h2>
          <span>最多 20 个合约 / 366 天</span>
        </div>
        <div className="factor-form-body">
          <label className="field-label factor-symbols">合约列表
            <input
              value={symbols}
              onChange={(event) => setSymbols(event.target.value)}
              spellCheck={false}
              required
            />
          </label>
          <div className="factor-form-grid">
            <label className="field-label">开始日期
              <input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} required />
            </label>
            <label className="field-label">结束日期
              <input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} required />
            </label>
            <label className="field-label">K线周期
              <select value={interval} onChange={(event) => setInterval(event.target.value as '1h' | '4h')}>
                <option value="1h">1 小时</option>
                <option value="4h">4 小时</option>
              </select>
            </label>
            <label className="field-label">未来收益跨度
              <select value={forwardBars} onChange={(event) => setForwardBars(Number(event.target.value))}>
                <option value={1}>1 根 K 线</option>
                <option value={4}>4 根 K 线</option>
                <option value={6}>6 根 K 线</option>
                <option value={24}>24 根 K 线</option>
              </select>
            </label>
            <label className="field-label">再平衡频率
              <select value={rebalanceBars} onChange={(event) => setRebalanceBars(Number(event.target.value))}>
                <option value={1}>每根 K 线</option>
                <option value={4}>每 4 根</option>
                <option value={6}>每 6 根</option>
                <option value={24}>每 24 根</option>
              </select>
            </label>
          </div>
          {research.error ? <div className="inline-error" role="alert">{research.error.message}</div> : null}
          {runs.error ? <div className="inline-error" role="alert">{runs.error.message}</div> : null}
          {runError ? <div className="inline-error" role="alert">{runError}</div> : null}
          {awaitingActiveRun ? <div className="factor-run-state queued">
            <LoaderCircle className="spin" size={15} />
            <div><strong>{runStatusLabels.QUEUED}</strong><small>正在同步任务状态</small></div>
          </div> : activeRun && !runError ? <div className={`factor-run-state ${activeRun.status.toLowerCase()}`}>
            {runPending ? <LoaderCircle className="spin" size={15} /> : <CheckCircle2 size={15} />}
            <div><strong>{runStatusLabels[activeRun.status]}</strong><small>任务 {activeRun.id.slice(0, 8)}</small></div>
          </div> : null}
          <Button
            type="submit"
            variant="primary"
            icon={research.isPending || runPending ? <LoaderCircle className="spin" size={15} /> : <FlaskConical size={15} />}
            disabled={research.isPending || runPending}
          >
            {research.isPending ? '正在创建任务...' : runPending ? '研究任务运行中' : '运行因子研究'}
          </Button>
        </div>
      </form>

      <div className="factor-data-panel">
        <div className="section-head factor-section-head">
          <h2><Database size={15} /> 数据覆盖</h2>
          <span>{catalog.data?.market_source ?? '公共行情源'}</span>
        </div>
        <div className="factor-source-list">
          {catalog.isLoading ? <div className="factor-source-loading">正在读取数据能力...</div> : sources.map((source) => (
            <div className="factor-source-row" key={source.key}>
              <span className={`source-state ${source.available ? 'available' : 'missing'}`} />
              <div><strong>{source.label}</strong><small>{source.detail}</small></div>
              <span>{source.available ? '可用' : '缺失'}</span>
            </div>
          ))}
        </div>
        <div className="factor-boundary-note">
          <AlertTriangle size={15} />
          <span>历史 OI、基差与盘口未接入时，对应因子显示数据缺失，不以零值替代。</span>
        </div>
      </div>
    </section>

    {result ? <>
      <section className="factor-metric-strip" aria-label="研究摘要">
        <div><span>评估因子</span><strong>{result.summary.factor_count}</strong></div>
        <div><span>通过</span><strong className="positive">{result.summary.passed}</strong></div>
        <div><span>观察</span><strong className="warning">{result.summary.watch}</strong></div>
        <div><span>样本不足</span><strong>{result.summary.insufficient}</strong></div>
        <div><span>数据缺失</span><strong>{result.summary.unavailable}</strong></div>
        <div><span>评估时间点</span><strong>{result.summary.timestamps_evaluated}</strong></div>
      </section>

      <section className="surface factor-results">
        <div className="section-head">
          <h2>研究结果</h2>
          <span>{result.parameters.symbols.length} 个合约 · {result.parameters.interval} · {result.parameters.start_date} 至 {result.parameters.end_date}</span>
        </div>
        <div className="table-scroll">
          <table className="data-table factor-table">
            <thead><tr>
              <th>因子</th><th>分类</th><th>状态</th><th>Mean IC</th><th>ICIR</th>
              <th>IC 正值率</th><th>样本内 IC</th><th>样本外 IC</th><th>q 值</th>
              <th>换手率</th><th>目标期 IC</th><th>WF净收益</th><th>WF最大回撤</th><th>WF Sharpe</th><th>时间点</th>
            </tr></thead>
            <tbody>{result.factors.map((factor) => <tr key={factor.key}>
              <td className="factor-name-cell"><strong>{factor.label}</strong><small>{factor.unavailable_reason ?? factor.description}</small></td>
              <td>{factor.category}</td>
              <td><span className={`factor-status ${factor.status.toLowerCase()}`}>{statusLabels[factor.status]}</span></td>
              <td className={factor.mean_ic !== null && factor.mean_ic < 0 ? 'negative mono' : 'mono'}>{formatMetric(factor.mean_ic)}</td>
              <td className="mono">{formatMetric(factor.icir)}</td>
              <td className="mono">{formatPercent(factor.positive_ic_rate)}</td>
              <td className="mono">{formatMetric(factor.in_sample_ic)}</td>
              <td className="mono">{formatMetric(factor.out_of_sample_ic)}</td>
              <td className="mono">{formatMetric(factor.q_value)}</td>
              <td className="mono">{formatPercent(factor.turnover)}</td>
              <td className="mono">{latestDecay(factor)}</td>
              <td className={portfolioMetric(preferredPortfolio(factor), 'net_return') !== null && (portfolioMetric(preferredPortfolio(factor), 'net_return') ?? 0) < 0 ? 'negative mono' : 'mono'}>{formatPercent(portfolioMetric(preferredPortfolio(factor), 'net_return'))}</td>
              <td className="mono">{formatPercent(portfolioMetric(preferredPortfolio(factor), 'max_drawdown'))}</td>
              <td className="mono">{formatMetric(portfolioMetric(preferredPortfolio(factor), 'sharpe'))}</td>
              <td className="mono">{factor.timestamp_count}</td>
            </tr>)}</tbody>
          </table>
        </div>
        <div className="factor-methodology">
          <span>Spearman Rank IC</span>
          <span>横截面 5% Winsorize + z-score</span>
          <span>滚动 Walk-forward 样本外验证</span>
          <span>taker 费率 + 滑点 + 已知资金费率</span>
          <span>BH-FDR q≤0.05</span>
          <strong>{result.methodology.pass_rule}</strong>
        </div>
      </section>
      {shadow.data?.[0] ? <section className="surface factor-shadow-results">
        <div className="section-head">
          <h2>影子因子排名</h2>
          <span>80% 原排名 + 20% 因子排名；SHADOW 不影响订单</span>
        </div>
        <div className="factor-shadow-meta">
          <span>研究任务 {shadow.data[0].research_run_id.slice(0, 8)}</span>
          <span>{shadow.data[0].payload.selected_factors.length} 个通过因子</span>
          <span>{shadow.data[0].timestamp}</span>
        </div>
        <div className="table-scroll">
          <table className="data-table factor-shadow-table">
            <thead><tr><th>综合排名</th><th>合约</th><th>原排名</th><th>因子排名</th><th>综合分</th><th>风险倍率</th><th>覆盖率</th><th>因子贡献</th></tr></thead>
            <tbody>{shadow.data[0].payload.rankings.map((row) => <tr key={row.symbol}>
              <td className="mono">{row.combined_rank}</td>
              <td><strong>{row.symbol}</strong></td>
              <td className="mono">{row.baseline_rank}</td>
              <td className="mono">{row.factor_rank}</td>
              <td className="mono">{formatMetric(Number(row.combined_score))}</td>
              <td className="mono">{Number(row.risk_multiplier).toFixed(2)}×</td>
              <td className="mono">{formatPercent(Number(row.factor_coverage))}</td>
              <td className="factor-contributions">{Object.entries(row.contributions).map(([key, value]) => { const numeric = Number(value); return `${key} ${numeric >= 0 ? '+' : ''}${numeric.toFixed(2)}` }).join(' · ')}</td>
            </tr>)}</tbody>
          </table>
        </div>
      </section> : null}
    </> : <section className="surface factor-empty">
      <FlaskConical size={22} />
      <strong>尚未运行研究</strong>
      <span>{catalog.data?.factors.length ?? 12} 个候选因子等待评估</span>
    </section>}
  </div>
}
