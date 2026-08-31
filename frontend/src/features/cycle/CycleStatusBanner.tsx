import type { DashboardData } from '../../api/types'
import { formatTime } from '../signals/signalPresentation'

type CycleStatus = DashboardData['cycle_status']

const cycleStateLabel: Record<CycleStatus['state'], string> = {
  RUNNING: '本轮执行中',
  COMPLETED: '本轮已完成',
  EXECUTED: '本轮已有执行',
  NO_CANDIDATES: '本轮无候选，未调用模型',
  RISK_REJECTED: '模型已返回，但被硬风控拒绝',
  MODEL_UNAVAILABLE: '模型不可用，本轮未增险',
  MODEL_TIMEOUT: '模型响应超时，本轮未增险',
  MODEL_THROTTLED: '模型节流中，本轮未重复调用',
  BLOCKED_RECONCILIATION: '需先完成仓位对账，本轮未调用模型',
  EXCHANGE_UNAVAILABLE: '交易所暂不可用，本轮未调用模型',
  WORKER_BUSY: '上一轮仍在执行，本轮未重复启动',
  WORKER_INTERRUPTED: 'Worker 中断，本轮未增险',
  FAILED: '本轮异常，已安全停止',
  UNKNOWN: '等待 Worker 状态',
}

export function CycleStatusBanner({ status }: { status: CycleStatus }) {
  const timestamp = status.finished_at ?? status.started_at
  return (
    <div className={'cycle-status cycle-status-' + status.state.toLowerCase()} role="status">
      <strong>{cycleStateLabel[status.state] ?? status.state}</strong>
      <span>{status.detail}</span>
      <small>
        快照 {status.snapshots} · 候选 {status.candidates} · 模型输出 {status.signals} · 执行 {status.executed}
        {timestamp ? ` · 更新时间 ${formatTime(timestamp)}` : ''}
      </small>
    </div>
  )
}

export { cycleStateLabel }
