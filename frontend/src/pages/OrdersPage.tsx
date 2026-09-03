import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { EmptyState } from '../components/EmptyState'
import { PageHeader } from '../components/PageHeader'
import { PageError, PageLoading } from '../components/PageState'
import { formatTime } from '../features/signals/signalPresentation'

export default function OrdersPage() {
  const orders = useQuery({ queryKey: ['orders'], queryFn: api.orders, refetchInterval: 10_000 })
  if (orders.isLoading) return <><PageHeader title="订单" subtitle="入场、止损、分批止盈与紧急退出" /><PageLoading /></>
  if (orders.isError) return <><PageHeader title="订单" subtitle="入场、止损、分批止盈与紧急退出" /><PageError message={orders.error.message} retry={() => orders.refetch()} /></>
  return <><PageHeader title="订单" subtitle="入场、止损、分批止盈与紧急退出" /><section className="surface page-surface"><div className="section-head"><h2>订单流水</h2><span>{orders.data?.length ?? 0} 条</span></div>{!orders.data?.length ? <EmptyState title="暂无订单记录" detail="执行状态机会在此记录每次提交、成交、撤单和保护单。" /> : <div className="table-scroll"><table className="data-table"><thead><tr><th>更新时间</th><th>合约</th><th>类型</th><th>方向</th><th>数量</th><th>成交数量</th><th>价格 / 触发价</th><th>状态</th><th>客户端订单号</th></tr></thead><tbody>{orders.data.map((order) => <tr key={order.client_order_id}><td>{formatTime(order.updated_at, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })}</td><td><strong>{order.symbol}</strong></td><td>{order.order_type}</td><td>{order.position_side} · {order.side}</td><td className="mono">{order.quantity}</td><td className="mono">{order.filled_quantity}</td><td className="mono">{order.price || order.stop_price || '市价'}</td><td>{order.status}</td><td className="mono muted">{order.client_order_id}</td></tr>)}</tbody></table></div>}</section></>
}
