import { ArrowDownRight, ArrowUpRight, CircleDollarSign, Coins, Gauge, Receipt, Wallet } from 'lucide-react'

const number = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export function MetricStrip({ metrics }: { metrics: { equity: string; daily_pnl: string; realized_pnl?: string; fees_today?: string; funding_today?: string; drawdown_pct: string; margin_pct: string } }) {
  const dailyPnl = Number(metrics.daily_pnl)
  const items = [
    { label: '账户净值', value: `${number.format(Number(metrics.equity))} USDT`, icon: CircleDollarSign, tone: '' },
    { label: '今日 PnL', value: `${dailyPnl >= 0 ? '+' : ''}${number.format(dailyPnl)} USDT`, icon: dailyPnl >= 0 ? ArrowUpRight : ArrowDownRight, tone: dailyPnl >= 0 ? 'positive' : 'negative' },
    { label: '已实现 PnL', value: `${number.format(Number(metrics.realized_pnl ?? '0'))} USDT`, icon: Wallet, tone: '' },
    { label: '手续费', value: `${number.format(Number(metrics.fees_today ?? '0'))} USDT`, icon: Receipt, tone: '' },
    { label: '资金费', value: `${number.format(Number(metrics.funding_today ?? '0'))} USDT`, icon: Coins, tone: Number(metrics.funding_today ?? '0') >= 0 ? 'positive' : 'negative' },
    { label: '当前回撤', value: `${(Number(metrics.drawdown_pct) * 100).toFixed(2)}%`, icon: ArrowDownRight, tone: 'warning' },
    { label: '保证金占用', value: `${(Number(metrics.margin_pct) * 100).toFixed(1)}%`, icon: Gauge, tone: '' },
  ]
  return (
    <section className="metric-strip" aria-label="账户指标">
      {items.map(({ label, value, icon: Icon, tone }) => (
        <div className="metric-cell" key={label}>
          <div className="metric-label"><Icon size={15} />{label}</div>
          <strong className={tone}>{value}</strong>
        </div>
      ))}
    </section>
  )
}
