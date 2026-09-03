import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { Position } from '../api/types'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { PageHeader } from '../components/PageHeader'
import { PageError, PageLoading } from '../components/PageState'
import { PositionsTable } from '../features/dashboard/PositionsTable'

export default function PositionsPage() {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<Position | null>(null)
  const [fraction, setFraction] = useState(0.5)
  const positions = useQuery({ queryKey: ['positions'], queryFn: api.positions, refetchInterval: 10_000 })
  const config = useQuery({ queryKey: ['config'], queryFn: api.config })
  const reduce = useMutation({
    mutationFn: (password: string) => api.reducePosition(selected?.position_id ?? '', fraction, password),
    onSuccess: () => {
      setSelected(null)
      queryClient.invalidateQueries({ queryKey: ['positions'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard'] })
    },
  })
  if (positions.isLoading) return <><PageHeader title="仓位" subtitle="双向逐仓 · 同币禁止同时多空" /><PageLoading /></>
  if (positions.isError) return <><PageHeader title="仓位" subtitle="双向逐仓 · 同币禁止同时多空" /><PageError message={positions.error.message} retry={() => positions.refetch()} /></>
  const totalPnl = (positions.data ?? []).reduce((sum, item) => sum + Number(item.unrealized_pnl), 0)
  const maxPositions = config.data?.max_positions ?? '—'
  const maxSameDirection = config.data?.max_same_direction ?? '—'
  return <><PageHeader title="仓位" subtitle="双向逐仓 · 同币禁止同时多空" /><section className="metric-strip compact"><div className="metric-cell"><div className="metric-label">当前仓位</div><strong>{positions.data?.length ?? 0} / {maxPositions}</strong></div><div className="metric-cell"><div className="metric-label">同向上限</div><strong>{maxSameDirection}</strong></div><div className="metric-cell"><div className="metric-label">未实现盈亏</div><strong className={totalPnl >= 0 ? 'positive' : 'negative'}>{totalPnl >= 0 ? '+' : ''}{totalPnl.toFixed(2)} USDT</strong></div></section><section className="surface page-surface"><div className="section-head"><h2>受管仓位</h2><span>保护单实时对账</span></div><PositionsTable positions={positions.data ?? []} onReduce={(position) => { setSelected(position); setFraction(0.5) }} /></section><ConfirmDialog open={Boolean(selected)} title={`减仓 ${selected?.symbol ?? ''}`} body="系统将以市价减少该方向仓位，并重新核对剩余仓位的交易所端硬止损。" confirmLabel="确认减仓" requirePassword busy={reduce.isPending} error={reduce.error?.message} onClose={() => setSelected(null)} onConfirm={(password) => reduce.mutate(password)}><label className="field-label">减仓比例<select value={fraction} onChange={(event) => setFraction(Number(event.target.value))}><option value={0.25}>25%</option><option value={0.5}>50%</option><option value={1}>100%</option></select></label></ConfirmDialog></>
}
