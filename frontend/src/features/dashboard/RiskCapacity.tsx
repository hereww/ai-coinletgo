interface MeterProps { label: string; used: string; limit: string; format: 'risk' | 'margin' }

function Meter({ label, used, limit, format }: MeterProps) {
  const ratio = Math.min(1, Number(used) / Number(limit))
  const pct = (value: string) => `${(Number(value) * 100).toFixed(format === 'risk' ? 2 : 1)}%`
  return (
    <div className="risk-meter">
      <div className="risk-meter-head"><span>{label}</span><span>{pct(used)} / {pct(limit)}</span></div>
      <div className="risk-track"><span style={{ width: `${ratio * 100}%` }} /></div>
    </div>
  )
}

export function RiskCapacity({ data }: { data: { single_trade: { used: string; limit: string }; portfolio: { used: string; limit: string }; margin: { used: string; limit: string }; max_leverage: number; positions: { used: number; limit: number } } }) {
  return (
    <div className="risk-list">
      <Meter label="单笔风险" {...data.single_trade} format="risk" />
      <Meter label="组合初始风险" {...data.portfolio} format="risk" />
      <Meter label="保证金占用" {...data.margin} format="margin" />
      <div className="limit-row"><span>杠杆上限</span><strong>{data.max_leverage}×</strong></div>
      <div className="limit-row"><span>仓位数量</span><strong>{data.positions.used} / {data.positions.limit}</strong></div>
    </div>
  )
}
