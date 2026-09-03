import { Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { formatTime } from '../signals/signalPresentation'

export function EquityChart({ data }: { data: Array<{ time: string; equity: string; drawdown: string }> }) {
  if (data.length < 2) {
    return <div className="chart-wrap chart-empty" role="img" aria-label="净值与回撤图：暂无净值历史">暂无净值历史</div>
  }
  const rows = data.map((item) => ({
    ...item,
    label: formatTime(item.time, { hour: '2-digit', minute: '2-digit' }),
    equity: Number(item.equity),
    drawdown: -Number(item.drawdown),
  }))
  return (
    <div className="chart-wrap" role="img" aria-label="净值与回撤图">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 2 }}>
          <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
          <XAxis dataKey="label" tick={{ fill: 'var(--muted)', fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis yAxisId="equity" domain={['dataMin - 2', 'dataMax + 2']} tick={{ fill: 'var(--muted)', fontSize: 11 }} tickLine={false} axisLine={false} width={46} />
          <YAxis yAxisId="drawdown" orientation="right" tick={false} axisLine={false} width={10} />
          <Tooltip contentStyle={{ border: '1px solid var(--border)', borderRadius: 6, boxShadow: 'none', fontSize: 12 }} />
          <Area yAxisId="drawdown" type="monotone" dataKey="drawdown" fill="var(--red-chart-fill)" stroke="var(--red-chart)" strokeWidth={1} />
          <Line yAxisId="equity" type="monotone" dataKey="equity" stroke="var(--green)" strokeWidth={2} dot={false} activeDot={{ r: 3 }} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}
