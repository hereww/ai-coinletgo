import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { EmptyState } from '../components/EmptyState'
import { PageHeader } from '../components/PageHeader'
import { PageError, PageLoading } from '../components/PageState'
import { formatTime } from '../features/signals/signalPresentation'

export default function AuditPage() {
  const audit = useQuery({ queryKey: ['audit'], queryFn: api.audit, refetchInterval: 30_000 })
  if (audit.isLoading) return <><PageHeader title="审计" subtitle="登录、配置、模式和紧急操作记录" /><PageLoading /></>
  if (audit.isError) return <><PageHeader title="审计" subtitle="登录、配置、模式和紧急操作记录" /><PageError message={audit.error.message} retry={() => audit.refetch()} /></>
  return <><PageHeader title="审计" subtitle="登录、配置、模式和紧急操作记录" /><section className="surface page-surface"><div className="section-head"><h2>不可变事件流</h2><span>{audit.data?.length ?? 0} 条</span></div>{!audit.data?.length ? <EmptyState title="暂无审计事件" detail="关键操作将记录操作者、来源和结果。" /> : <div className="table-scroll"><table className="data-table"><thead><tr><th>时间</th><th>操作者</th><th>动作</th><th>资源</th><th>结果</th><th>来源 IP</th><th>详情</th></tr></thead><tbody>{audit.data.map((event) => <tr key={event.id}><td>{formatTime(event.created_at, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })}</td><td>{event.actor}</td><td>{event.action}</td><td>{event.resource}</td><td><span className={`result-text ${event.outcome}`}>{event.outcome}</span></td><td className="mono">{event.ip_address ?? '—'}</td><td className="muted">{Object.keys(event.detail).length ? JSON.stringify(event.detail) : '—'}</td></tr>)}</tbody></table></div>}</section></>
}
