import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { api } from '../api/client'
import type { DailyPnlRow, IncomeLedgerRow, OrderRow, TradePnlRow } from '../api/types'
import { Button } from '../components/Button'
import { EmptyState } from '../components/EmptyState'
import { PageHeader } from '../components/PageHeader'
import { PageError, PageLoading } from '../components/PageState'
import { APP_TIME_ZONE, formatNumber, formatTime } from '../features/signals/signalPresentation'

function appDate(offsetDays = 0) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: APP_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date())
  const value = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value ?? ''
  const localMidnight = new Date(Date.UTC(Number(value('year')), Number(value('month')) - 1, Number(value('day'))))
  localMidnight.setUTCDate(localMidnight.getUTCDate() + offsetDays)
  return localMidnight.toISOString().slice(0, 10)
}

function pnlClass(value: number) {
  if (value > 0) return 'positive'
  if (value < 0) return 'negative'
  return 'muted'
}

function signedNumber(value: number, maximumFractionDigits = 4) {
  const formatted = formatNumber(value, maximumFractionDigits)
  return value > 0 ? `+${formatted}` : formatted
}

export default function OrdersPage() {
  const queryClient = useQueryClient()
  const [startDate, setStartDate] = useState(() => appDate(-1))
  const [endDate, setEndDate] = useState(() => appDate())
  const validRange = startDate.length === 10 && endDate.length === 10 && startDate <= endDate
  const queryOptions = { enabled: validRange }
  const orders = useQuery({ queryKey: ['orders'], queryFn: api.orders, refetchInterval: 10_000 })
  const daily = useQuery({ queryKey: ['pnl', 'daily', startDate, endDate], queryFn: () => api.pnlDaily(startDate, endDate), ...queryOptions })
  const trades = useQuery({ queryKey: ['pnl', 'trades', startDate, endDate], queryFn: () => api.pnlTrades(startDate, endDate), ...queryOptions })
  const ledger = useQuery({ queryKey: ['pnl', 'ledger', startDate, endDate], queryFn: () => api.pnlLedger(startDate, endDate), ...queryOptions })
  const sync = useMutation({
    mutationFn: () => api.syncPnl({ start_date: startDate, end_date: endDate }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['pnl'] })
    },
  })
  const totals = useMemo(() => (daily.data ?? []).reduce((result, row) => ({
    realized: result.realized + Number(row.realized_pnl),
    commission: result.commission + Number(row.commission),
    funding: result.funding + Number(row.funding_fee),
    net: result.net + Number(row.net_pnl),
  }), { realized: 0, commission: 0, funding: 0, net: 0 }), [daily.data])

  if (orders.isLoading && daily.isLoading && trades.isLoading && ledger.isLoading) {
    return <><PageHeader title="订单与盈亏" subtitle="以 Binance 不可变收入台账归集 · 北京时间" /><PageLoading /></>
  }

  const pnlError = daily.error ?? trades.error ?? ledger.error
  return <>
    <PageHeader
      title="订单与盈亏"
      subtitle="以 Binance 不可变收入台账归集 · 北京时间"
      actions={<>
        <label className="pnl-date-field">开始<input aria-label="开始日期" type="date" value={startDate} max={endDate} onChange={(event) => setStartDate(event.target.value)} /></label>
        <label className="pnl-date-field">结束<input aria-label="结束日期" type="date" value={endDate} min={startDate} onChange={(event) => setEndDate(event.target.value)} /></label>
        <Button variant="primary" icon={<RefreshCw className={sync.isPending ? 'spin' : undefined} size={15} />} disabled={!validRange || sync.isPending} onClick={() => sync.mutate()}>{sync.isPending ? '同步中...' : '同步交易所台账'}</Button>
      </>}
    />
    {!validRange ? <div className="inline-error" role="alert">结束日期不能早于开始日期。</div> : null}
    {sync.error ? <div className="inline-error" role="alert">{sync.error.message}</div> : null}
    {sync.isSuccess ? <div className="success-note" role="status">已同步 {sync.data.fetched_rows} 条交易所台账，新增 {sync.data.inserted_rows} 条记录。</div> : null}
    {pnlError ? <div className="inline-error" role="alert">{pnlError.message}</div> : null}

    <section className="metric-strip pnl-metric-strip">
      <div className="metric-cell"><div className="metric-label">已实现盈亏</div><strong className={pnlClass(totals.realized)}>{signedNumber(totals.realized)} USDT</strong></div>
      <div className="metric-cell"><div className="metric-label">手续费</div><strong className={pnlClass(totals.commission)}>{signedNumber(totals.commission)} USDT</strong></div>
      <div className="metric-cell"><div className="metric-label">资金费</div><strong className={pnlClass(totals.funding)}>{signedNumber(totals.funding)} USDT</strong></div>
      <div className="metric-cell"><div className="metric-label">净盈亏</div><strong className={pnlClass(totals.net)}>{signedNumber(totals.net)} USDT</strong></div>
    </section>

    <section className="surface pnl-surface">
      <div className="section-head"><h2>每日盈亏</h2><span>{daily.data?.length ?? 0} 天 · 已实现 + 手续费 + 资金费</span></div>
      <DailyPnlTable rows={daily.data ?? []} loading={daily.isLoading} />
    </section>

    <section className="surface pnl-surface">
      <div className="section-head"><h2>成交关联盈亏</h2><span>{trades.data?.length ?? 0} 笔 · 按交易所 trade ID 聚合</span></div>
      <TradePnlTable rows={trades.data ?? []} loading={trades.isLoading} />
    </section>

    <section className="surface pnl-surface">
      <div className="section-head"><h2>结算明细</h2><span>{ledger.data?.length ?? 0} 条 · 含无 trade ID 的资金费</span></div>
      <IncomeLedgerTable rows={ledger.data ?? []} loading={ledger.isLoading} />
    </section>

    <section className="surface page-surface">
      <div className="section-head"><h2>订单流水</h2><span>{orders.data?.length ?? 0} 条</span></div>
      {orders.isError ? <PageError message={orders.error.message} retry={() => orders.refetch()} /> : <OrdersTable rows={orders.data ?? []} />}
    </section>
  </>
}

function DailyPnlTable({ rows, loading }: { rows: DailyPnlRow[]; loading: boolean }) {
  if (loading) return <PageLoading />
  if (!rows.length) return <EmptyState title="所选日期暂无已归集盈亏" detail="点击“同步交易所台账”后，会从 Binance 拉取已实现盈亏、手续费和资金费。" />
  return <div className="table-scroll"><table className="data-table pnl-table"><thead><tr><th>日期</th><th>资产</th><th>已实现</th><th>手续费</th><th>资金费</th><th>净盈亏</th><th>明细数</th></tr></thead><tbody>{rows.map((row) => <tr key={`${row.date}-${row.asset}`}><td className="mono">{row.date}</td><td>{row.asset}</td><td className={`mono ${pnlClass(Number(row.realized_pnl))}`}>{signedNumber(Number(row.realized_pnl))}</td><td className={`mono ${pnlClass(Number(row.commission))}`}>{signedNumber(Number(row.commission))}</td><td className={`mono ${pnlClass(Number(row.funding_fee))}`}>{signedNumber(Number(row.funding_fee))}</td><td className={`mono ${pnlClass(Number(row.net_pnl))}`}>{signedNumber(Number(row.net_pnl))}</td><td className="mono muted">{row.event_count}</td></tr>)}</tbody></table></div>
}

function TradePnlTable({ rows, loading }: { rows: TradePnlRow[]; loading: boolean }) {
  if (loading) return <PageLoading />
  if (!rows.length) return <EmptyState title="所选日期暂无成交关联盈亏" detail="这里按 Binance trade ID 汇总；资金费等无 trade ID 的结算会保留在下方明细中。" />
  return <div className="table-scroll"><table className="data-table pnl-table"><thead><tr><th>最后结算时间</th><th>合约</th><th>交易 ID</th><th>已实现</th><th>手续费</th><th>资金费</th><th>净盈亏</th><th>事件数</th></tr></thead><tbody>{rows.map((row) => <tr key={`${row.symbol}-${row.trade_id}-${row.asset}`}><td>{formatTime(row.last_event_at, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })}</td><td><strong>{row.symbol}</strong></td><td className="mono muted">{row.trade_id}</td><td className={`mono ${pnlClass(Number(row.realized_pnl))}`}>{signedNumber(Number(row.realized_pnl))}</td><td className={`mono ${pnlClass(Number(row.commission))}`}>{signedNumber(Number(row.commission))}</td><td className={`mono ${pnlClass(Number(row.funding_fee))}`}>{signedNumber(Number(row.funding_fee))}</td><td className={`mono ${pnlClass(Number(row.net_pnl))}`}>{signedNumber(Number(row.net_pnl))} {row.asset}</td><td className="mono muted">{row.event_count}</td></tr>)}</tbody></table></div>
}

function IncomeLedgerTable({ rows, loading }: { rows: IncomeLedgerRow[]; loading: boolean }) {
  if (loading) return <PageLoading />
  if (!rows.length) return <EmptyState title="所选日期暂无结算台账" detail="同步后会在此保留从交易所拉取的不可变收入记录。" />
  return <div className="table-scroll"><table className="data-table pnl-table"><thead><tr><th>结算时间</th><th>合约</th><th>类型</th><th>金额</th><th>资产</th><th>交易 ID</th><th>台账 ID</th></tr></thead><tbody>{rows.map((row) => <tr key={row.income_id}><td>{formatTime(row.event_time, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })}</td><td><strong>{row.symbol || '—'}</strong></td><td>{row.income_type}</td><td className={`mono ${pnlClass(Number(row.income))}`}>{signedNumber(Number(row.income))}</td><td>{row.asset}</td><td className="mono muted">{row.trade_id ?? '—'}</td><td className="mono muted">{row.income_id}</td></tr>)}</tbody></table></div>
}

function OrdersTable({ rows }: { rows: OrderRow[] }) {
  if (!rows.length) return <EmptyState title="暂无订单记录" detail="执行状态机会在此记录每次提交、成交、撤单和保护单。" />
  return <div className="table-scroll"><table className="data-table"><thead><tr><th>更新时间</th><th>合约</th><th>类型</th><th>方向</th><th>数量</th><th>成交数量</th><th>价格 / 触发价</th><th>状态</th><th>客户端订单号</th></tr></thead><tbody>{rows.map((order) => <tr key={order.client_order_id}><td>{formatTime(order.updated_at, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })}</td><td><strong>{order.symbol}</strong></td><td>{order.order_type}</td><td>{order.position_side} · {order.side}</td><td className="mono">{order.quantity}</td><td className="mono">{order.filled_quantity}</td><td className="mono">{order.price || order.stop_price || '市价'}</td><td>{order.status}</td><td className="mono muted">{order.client_order_id}</td></tr>)}</tbody></table></div>
}
