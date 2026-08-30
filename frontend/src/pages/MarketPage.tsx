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
        {!market.data?.length ? <EmptyState title="等待首轮市场快照" detail="Worker 完成一次 15 分钟周期后，此处显示筛选特征。" /> : (
          <div className="table-scroll"><table className="data-table"><thead><tr><th>合约</th><th>标记价格</th><th>24h 成交额</th><th>点差</th><th>资金费率</th><th>持仓量变化</th><th>ADX</th><th>1h / 4h</th><th>触发</th><th>评分</th></tr></thead><tbody>
            {market.data.map((row) => <tr key={row.symbol}><td><strong>{row.symbol}</strong></td><td className="mono">{Number(row.mark_price).toLocaleString()}</td><td className="mono">{money.format(Number(row.quote_volume_24h))}</td><td className="mono">{(Number(row.spread_pct) * 100).toFixed(3)}%</td><td className={`mono ${Math.abs(Number(row.funding_rate)) > 0.001 ? 'negative' : ''}`}>{(Number(row.funding_rate) * 100).toFixed(4)}%</td><td className="mono">{(Number(row.open_interest_change_pct) * 100).toFixed(2)}%</td><td className="mono">{Number(row.adx_1h).toFixed(1)}</td><td>{trend(row.trend_1h)} / {trend(row.trend_4h)}</td><td>{row.breakout_15m === 1 ? '向上突破' : row.breakout_15m === -1 ? '向下突破' : '观察'}</td><td className="mono">{Number(row.score).toFixed(2)}</td></tr>)}
          </tbody></table></div>
        )}
      </section>
    </>
  )
}

function trend(value: number) { return value === 1 ? '多' : value === -1 ? '空' : '中性' }
