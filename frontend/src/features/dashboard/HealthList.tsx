import { CheckCircle2, CircleAlert, CircleDashed } from 'lucide-react'
import type { HealthComponent } from '../../api/types'

const labels: Record<string, string> = {
  binance: '币安行情与账户',
  model_relay: '模型中转',
  database: '数据库',
  redis: 'Redis 状态',
  authentication: '认证配置',
  worker: '交易 Worker',
}

export function HealthList({ components }: { components: HealthComponent[] }) {
  return (
    <div className="health-list">
      {components.map((component) => {
        const healthy = component.state === 'HEALTHY'
        const Icon = healthy ? CheckCircle2 : component.state === 'NOT_CONFIGURED' ? CircleDashed : CircleAlert
        return (
          <div className="health-row" key={component.name}>
            <Icon size={16} className={healthy ? 'healthy-text' : 'warning-text'} />
            <div className="health-copy">
              <span>{labels[component.name] ?? component.name}</span>
              {component.detail ? <small>{component.detail}</small> : null}
            </div>
            <strong>{healthy ? '正常' : component.state === 'NOT_CONFIGURED' ? '未配置' : '异常'}</strong>
            <small className="health-latency">{component.latency_ms != null ? `${component.latency_ms} ms` : '—'}</small>
          </div>
        )
      })}
    </div>
  )
}
