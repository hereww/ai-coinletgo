import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { EmptyState } from '../components/EmptyState'
import { PageHeader } from '../components/PageHeader'
import { PageError, PageLoading } from '../components/PageState'

const money = new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 })

export default function MarketPage() {
  const market = useQuery({ queryKey: ['market'], queryFn: api.market, refetchInterval: 30_000 })
  if (market.isLoading) return <><PageHeader title="市场" subtitle="动态流动性前 30 · 15m / 1h / 4h" /><PageLoading /></>
  if (market.isError) return <><PageHeader title="市场" subtitle="动态流动性前 30 · 15m / 1h / 4h" /><PageError message={market.error.message} retry={() => market.refetch()} /></>
  return (
    <>
      <PageHeader title="市场" subtitle="动态流动性前 30 · 15m / 1h / 4h" />
      <section className="surface page-surface">
        <div className="section-head"><h2>候选交易池</h2><span>{market.data?.length ?? 0} 个合约</span></div>
        {!market.data?.length ? <EmptyState title="等待首轮市场快照" detail="Worker 完成一次扫描周期后，此处显示筛选特征。" /> : (
          <div className="table-scroll"><table className="data-table"><thead><tr><th>合约</th><th>标记价格</th><th>24h 成交额</th><th>点差</th><th>资金费率</th><th>持仓量变化</th><th>市场状态</th><th>ADX</th><th>1h / 4h</th><th>触发</th><th>风险系数</th><th>评分</th></tr></thead><tbody>
            {market.data.map((row) => <tr key={row.symbol}><td><strong>{row.symbol}</strong></td><td className="mono">{number(row.mark_price)}</td><td className="mono">{money.format(Number(row.quote_volume_24h))}</td><td className="mono">{percent(row.spread_pct, 3)}</td><td className={`mono ${Math.abs(Number(row.funding_rate)) > 0.001 ? 'negative' : ''}`}>{percent(row.funding_rate, 4)}</td><td className="mono">{percent(row.open_interest_change_pct, 2)}</td><td>{regime(row.market_regime)}</td><td className="mono">{number(row.adx_1h, 1)}</td><td>{trend(row.trend_1h)} / {trend(row.trend_4h)}</td><td>{trigger(row.breakout_15m, row.pullback_15m)}</td><td className="mono">{multiplier(row.volatility_risk_multiplier)}</td><td className="mono">{number(row.score, 2)}</td></tr>)}
          </tbody></table></div>
        )}
      </section>
    </>
  )
}

function trend(value: number) { return value === 1 ? '多' : value === -1 ? '空' : '中性' }
function regime(value?: string) { return value ? ({ TRENDING: '趋势', RANGING: '震荡', VOLATILE: '极端波动', UNCERTAIN: '不确定' } as Record<string, string>)[value] ?? '未知' : '历史数据未记录' }
function number(value: string | number | null | undefined, digits = 0) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed.toLocaleString(undefined, { maximumFractionDigits: digits }) : '历史数据未记录'
}
function percent(value: string | number | null | undefined, digits: number) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(digits)}%` : '—'
}
function multiplier(value?: string) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `${parsed.toFixed(2)}×` : '历史数据未记录'
}
function trigger(breakout: number, pullback?: number) {
  if (breakout === 1) return '向上突破'
  if (breakout === -1) return '向下突破'
  if (pullback === 1) return '多头回踩确认'
  if (pullback === -1) return '空头回踩确认'
  return '观察'
}
