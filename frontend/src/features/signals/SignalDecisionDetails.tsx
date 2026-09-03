import type { ReactNode } from 'react'
import type { Signal } from '../../api/types'
import { displayReason, formatNumber, formatPercent, formatTime, resultClass, resultLabel, trendLabel, triggerLabel } from './signalPresentation'

export function SignalDecisionDetails({ signal }: { signal: Signal }) {
  const market = signal.market_context
  const decision = signal.risk_decision

  return (
    <div className="signal-details" aria-label={`${signal.symbol} 决策详情`}>
      <DetailSection title="模型判断">
        <DetailMetric label="模型判断" value={signal.thesis || '该历史记录未保存模型判断。'} wide />
        <DetailMetric label="核心原因" value={<Tags values={signal.reason_codes_zh} fallback="该历史记录未保存原因标签。" />} wide />
        <DetailMetric label="风险标签" value={<Tags values={signal.risk_flags_zh} fallback="无额外风险标签" tone="risk" />} wide />
        <DetailMetric label="置信度" value={formatPercent(signal.confidence, 0)} />
        <DetailMetric label="分析周期" value={`${signal.horizon_minutes} 分钟`} />
        <DetailMetric label="有效至" value={signal.expires_at ? formatTime(signal.expires_at) : '该历史记录未保存'} />
      </DetailSection>

      <DetailSection title="交易结构">
        <DetailMetric label="入场区间" value={priceRange(signal.entry_min, signal.entry_max)} />
        <DetailMetric label="失效价 / 止损" value={formatNumber(signal.invalidation_price)} />
        <DetailMetric label="目标价" value={formatNumber(signal.target_price)} />
        <DetailMetric label="决策结论" value={<span className={`result-text ${resultClass(signal.result)}`}>{resultLabel(signal.result)}</span>} />
      </DetailSection>

      <DetailSection title="行情依据">
        {market ? <>
          <DetailMetric label="快照时间" value={formatTime(market.timestamp)} />
          <DetailMetric label="标记价格" value={formatNumber(market.mark_price)} />
          <DetailMetric label="1h / 4h 趋势" value={`${trendLabel(market.trend_1h)} / ${trendLabel(market.trend_4h)}`} />
          <DetailMetric label="15m 触发" value={`${triggerLabel(market.breakout_15m, 'breakout')} / ${triggerLabel(market.pullback_15m, 'pullback')}`} />
          <DetailMetric label="ADX / ATR" value={`${formatNumber(market.adx_1h, 1)} / ${formatNumber(market.atr_15m)}`} />
          <DetailMetric label="市场状态" value={regimeLabel(market.market_regime)} />
          <DetailMetric label="波动分位 / 风险系数" value={`${formatPercent(market.volatility_percentile, 0)} / ${formatNumber(market.volatility_risk_multiplier, 2)}×`} />
          <DetailMetric label="成交量 Z-score" value={formatNumber(market.volume_zscore, 2)} />
          <DetailMetric label="资金费率" value={formatPercent(market.funding_rate, 4)} />
          <DetailMetric label="持仓量变化" value={formatPercent(market.open_interest_change_pct, 2)} />
          <DetailMetric label="买卖点差" value={formatPercent(market.spread_pct, 3)} />
        </> : <MissingDetail>该历史记录未保存决策时的行情快照。</MissingDetail>}
      </DetailSection>

      <DetailSection title="硬风控">
        {decision ? <>
          <DetailMetric label="风控结果" value={<span className={`result-text ${decision.status === 'APPROVED' ? 'approved' : 'rejected'}`}>{decision.status === 'APPROVED' ? '已通过' : '已拒绝'}</span>} />
          <DetailMetric label="风控时间" value={formatTime(decision.decided_at)} />
          <DetailMetric label="风控原因" value={<Tags values={decision.reasons_zh} fallback="未返回风控原因" />} wide />
          {decision.status === 'APPROVED' ? <>
            <DetailMetric label="计算数量" value={formatNumber(decision.quantity)} />
            <DetailMetric label="杠杆" value={`${decision.leverage}×`} />
            <DetailMetric label="风险金额" value={`${formatNumber(decision.risk_amount_usdt, 2)} USDT`} />
            <DetailMetric label="动态风险系数" value={`${formatNumber(decision.risk_multiplier, 2)}×`} />
            <DetailMetric label="预计保证金" value={`${formatNumber(decision.estimated_margin, 2)} USDT`} />
            <DetailMetric label="风控入场 / 止损" value={`${formatNumber(decision.entry_price)} / ${formatNumber(decision.stop_price)}`} />
            <DetailMetric label="风控目标 / 净盈亏比" value={`${formatNumber(decision.target_price)} / ${formatRatio(decision.net_reward_risk)}R`} />
          </> : null}
        </> : <MissingDetail>该历史记录未保存硬风控结论。</MissingDetail>}
      </DetailSection>

      <p className="signal-detail-advice"><span>操作建议</span>{displayReason(signal)} {signal.recommendation_zh ?? signal.ai_advice ?? '建议等待下一轮周期确认。'}</p>
    </div>
  )
}

function regimeLabel(value: SignalMarketContextRegime | undefined) {
  if (!value) return '历史数据未记录'
  return ({ TRENDING: '趋势', RANGING: '震荡', VOLATILE: '极端波动', UNCERTAIN: '不确定' })[value] ?? '未知'
}

type SignalMarketContextRegime = NonNullable<Signal['market_context']>['market_regime']

function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return <section className="signal-detail-section"><h3>{title}</h3><div className="signal-detail-grid">{children}</div></section>
}

function DetailMetric({ label, value, wide = false }: { label: string; value: ReactNode; wide?: boolean }) {
  return <div className={`signal-detail-metric ${wide ? 'wide' : ''}`}><span>{label}</span><strong>{value}</strong></div>
}

function MissingDetail({ children }: { children: ReactNode }) {
  return <p className="signal-detail-missing">{children}</p>
}

function Tags({ values, fallback, tone }: { values: string[]; fallback: string; tone?: 'risk' }) {
  if (!values.length) return <span className="signal-detail-placeholder">{fallback}</span>
  return <span className="signal-tags">{values.map((value) => <span className={tone === 'risk' ? 'risk' : ''} key={value}>{value}</span>)}</span>
}

function priceRange(minimum: string | null, maximum: string | null) {
  if (minimum == null && maximum == null) return '不交易 / 未设入场区间'
  return `${formatNumber(minimum)} — ${formatNumber(maximum)}`
}

function formatRatio(value: string | null | undefined) {
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(2) : '—'
}
