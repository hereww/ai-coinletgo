import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, CheckCircle2, KeyRound, LogOut, Network, RefreshCw, Server, ShieldCheck, TestTube2, Wifi, XCircle } from 'lucide-react'
import { api } from '../api/client'
import type { IntegrationStatus } from '../api/types'
import { Button } from '../components/Button'
import { PageHeader } from '../components/PageHeader'
import { PageError, PageLoading } from '../components/PageState'
import { HealthList } from '../features/dashboard/HealthList'

export default function SettingsPage() {
  const queryClient = useQueryClient()
  const dashboard = useQuery({ queryKey: ['dashboard'], queryFn: api.dashboard, refetchInterval: 15_000 })
  const integrations = useQuery({ queryKey: ['integrations'], queryFn: api.integrations, refetchInterval: 15_000 })
  const [modelForm, setModelForm] = useState({ base_url: '', model_name: 'gpt-5.6', reasoning_effort: 'medium' as IntegrationStatus['model']['reasoning_effort'], timeout_seconds: 120, daily_request_limit: 110, strategy_profile: 'trend_following' as IntegrationStatus['model']['strategy_profile'] })
  const logout = useMutation({ mutationFn: api.logout, onSuccess: () => { queryClient.clear(); window.location.reload() } })
  const probeTestnet = useMutation({ mutationFn: () => api.probeIntegration('testnet'), onSuccess: () => integrations.refetch() })
  const probeModel = useMutation({ mutationFn: () => api.probeIntegration('model'), onSuccess: () => integrations.refetch() })
  const saveModel = useMutation({ mutationFn: () => api.updateModelIntegration({ ...modelForm, base_url: modelForm.base_url.trim() || null }), onSuccess: () => integrations.refetch() })
  const selectModel = useMutation({
    mutationFn: (profileId: IntegrationStatus['model']['active_profile']) => api.selectModelProfile(profileId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['dashboard'] })
      integrations.refetch()
    },
  })
  useEffect(() => {
    const model = integrations.data?.model
    const relay = model?.profiles.find((profile) => profile.id === 'relay')
    if (model && relay) setModelForm({ base_url: relay.base_url ?? '', model_name: relay.model_name, reasoning_effort: model.reasoning_effort, timeout_seconds: model.timeout_seconds, daily_request_limit: model.daily_request_limit, strategy_profile: model.strategy_profile })
  }, [integrations.data])
  if (dashboard.isLoading || integrations.isLoading) return <><PageHeader title="设置" subtitle="接入、运行环境与会话" /><PageLoading /></>
  if (dashboard.isError || integrations.isError || !dashboard.data || !integrations.data) return <><PageHeader title="设置" subtitle="接入、运行环境与会话" /><PageError message={dashboard.error?.message ?? integrations.error?.message ?? '系统状态不可用'} retry={() => { dashboard.refetch(); integrations.refetch() }} /></>
  const { data } = dashboard
  const binance = integrations.data.binance
  const model = integrations.data.model
  const proxy = integrations.data.proxy
  return <>
    <PageHeader title="设置" subtitle="接入、运行环境与会话" actions={<Button variant="ghost" icon={<RefreshCw size={15} />} onClick={() => { dashboard.refetch(); integrations.refetch() }}>刷新健康检查</Button>} />
    <section className="settings-page-grid">
      <div className="surface"><div className="section-head"><h2>系统健康</h2><Server size={18} /></div><HealthList components={data.health.components} /></div>
      <div className="surface runtime-info"><div className="section-head"><h2>运行环境</h2><ShieldCheck size={18} /></div><dl><div><dt>币安环境</dt><dd>{data.environment === 'testnet' ? '测试网' : '实盘'}</dd></div><div><dt>系统模式</dt><dd>{data.mode}</dd></div><div><dt>健康门禁</dt><dd className={data.health.ready ? 'positive' : 'warning'}>{data.health.ready ? '通过' : '未通过'}</dd></div><div><dt>运行时间</dt><dd>{formatUptime(data.uptime_seconds)}</dd></div><div><dt>数据时区</dt><dd>UTC</dd></div><div><dt>界面时区</dt><dd>Asia/Shanghai</dd></div></dl><Button variant="secondary" icon={<LogOut size={15} />} onClick={() => logout.mutate()} disabled={logout.isPending}>退出登录</Button></div>
    </section>
    <section className="integration-grid">
      <div className="surface integration-card">
        <div className="section-head"><h2><Network size={16} /> HTTP 代理模式</h2><StatusIcon healthy={proxy.enabled && proxy.configured} /></div>
        <div className="integration-body"><dl><div><dt>状态</dt><dd className={proxy.enabled && proxy.configured ? 'positive' : 'warning'}>{proxy.detail}</dd></div><div><dt>覆盖范围</dt><dd>{proxy.scope}</dd></div><div><dt>代理地址</dt><dd>{proxy.configured ? '已挂载 secret（地址已隐藏）' : '未配置'}</dd></div></dl><p className="setup-note"><KeyRound size={14} /> 代理地址不会进入页面、数据库或日志。当前部署可将代理仅用于 AI 中转，Binance 测试网保持直连；如需调整，请在服务器更新对应 secret 和 Binance 代理开关后重建 api/worker。</p></div>
      </div>
      <div className="surface integration-card">
        <div className="section-head"><h2><TestTube2 size={16} /> 币安测试网</h2><StatusIcon healthy={binance.configured && binance.health.state === 'HEALTHY'} /></div>
        <div className="integration-body"><dl><div><dt>环境</dt><dd>{binance.environment === 'testnet' ? 'U 本位测试网' : '当前不是测试网'}</dd></div><div><dt>API 密钥</dt><dd>{binance.configured ? '已挂载 secret' : '未配置'}</dd></div><div><dt>接口</dt><dd className="mono">{binance.base_url}</dd></div><div><dt>状态</dt><dd className={binance.health.state === 'HEALTHY' ? 'positive' : 'warning'}>{binance.health.detail ?? binance.health.state}</dd></div></dl><p className="setup-note"><KeyRound size={14} /> 密钥不会进入数据库。请在服务器 secrets 目录运行 <code>scripts/configure-testnet-secrets.sh</code>，再重建 api/worker。</p><Button icon={<Wifi size={15} />} onClick={() => probeTestnet.mutate()} disabled={probeTestnet.isPending}>{probeTestnet.isPending ? '测试中...' : '测试 Binance 连接'}</Button>{probeTestnet.error ? <div className="inline-error" role="alert">{probeTestnet.error.message}</div> : null}{probeTestnet.isSuccess ? <div className="success-note" role="status">{probeTestnet.data?.detail ?? '测试完成'}</div> : null}</div>
      </div>
      <div className="surface integration-card">
        <div className="section-head"><h2><Bot size={16} /> AI 模型 / Responses API</h2><StatusIcon healthy={model.configured && model.health.state === 'HEALTHY'} /></div>
        <div className="integration-body">
          <dl className="active-model-summary">
            <div><dt>当前配置</dt><dd className="positive">{model.active_label}</dd></div>
            <div><dt>当前模型</dt><dd className="mono">{model.model_name}</dd></div>
            <div><dt>当前接口</dt><dd className="mono">{model.base_url ?? '未配置'}</dd></div>
            <div><dt>API Key secret</dt><dd>{model.api_key_configured ? '已挂载' : '未配置'}</dd></div>
          </dl>

          <div className="model-profile-list" aria-label="AI 模型列表">
            {model.profiles.map((profile) => (
              <article className={`model-profile-option ${profile.active ? 'active' : ''}`} key={profile.id}>
                <div className="model-profile-head">
                  <div><strong>{profile.label}</strong><span>{profile.kind === 'self_hosted' ? '自建模型' : '中转服务'}</span></div>
                  <span className={`profile-state ${profile.configured ? 'ready' : ''}`}>{profile.configured ? '已配置' : '未配置'}</span>
                </div>
                <p className="mono">{profile.model_name}</p>
                <p className="muted mono">{profile.base_url ?? '尚未设置 Base URL'}</p>
                <Button
                  variant={profile.active ? 'secondary' : 'primary'}
                  onClick={() => selectModel.mutate(profile.id)}
                  disabled={profile.active || !profile.configured || selectModel.isPending}
                >
                  {profile.active ? '当前使用' : '切换到此模型'}
                </Button>
              </article>
            ))}
          </div>
          {selectModel.error ? <div className="inline-error" role="alert">{selectModel.error.message}</div> : null}
          {selectModel.isSuccess ? <div className="success-note" role="status">模型已切换，下个分析周期生效</div> : null}

          <div className="subsection-head"><h3>策略与中转配置</h3><span>模型切换不会改变风控参数</span></div>
          <div className="form-grid">
            <label className="field-label">OpenAI 中转 Base URL<input value={modelForm.base_url} onChange={(event) => setModelForm((value) => ({ ...value, base_url: event.target.value }))} placeholder="https://relay.example.com 或 .../v1" /></label>
            <label className="field-label">OpenAI 中转模型<input value={modelForm.model_name} onChange={(event) => setModelForm((value) => ({ ...value, model_name: event.target.value }))} /></label>
            <label className="field-label">AI 策略模板<select value={modelForm.strategy_profile} onChange={(event) => setModelForm((value) => ({ ...value, strategy_profile: event.target.value as IntegrationStatus['model']['strategy_profile'] }))}><option value="trend_following">趋势跟随（默认）</option><option value="balanced">平衡</option><option value="conservative">保守</option><option value="scalping">短线</option></select></label>
            <label className="field-label">推理强度<select value={modelForm.reasoning_effort} onChange={(event) => setModelForm((value) => ({ ...value, reasoning_effort: event.target.value as IntegrationStatus['model']['reasoning_effort'] }))}><option value="none">none</option><option value="low">low</option><option value="medium">medium</option><option value="high">high</option><option value="xhigh">xhigh</option><option value="max">max</option></select></label>
            <label className="field-label">超时（秒）<input type="number" min={2} max={120} value={modelForm.timeout_seconds} onChange={(event) => setModelForm((value) => ({ ...value, timeout_seconds: Number(event.target.value) }))} /></label>
            <label className="field-label">每日请求上限<input type="number" min={1} max={110} value={modelForm.daily_request_limit} onChange={(event) => setModelForm((value) => ({ ...value, daily_request_limit: Number(event.target.value) }))} /></label>
          </div>
          <dl><div><dt>结构化探针</dt><dd className={model.health.state === 'HEALTHY' ? 'positive' : 'warning'}>{model.health.detail ?? model.health.state}</dd></div></dl>
          <p className="setup-note"><KeyRound size={14} /> 每个模型使用独立的服务器 secret；API Key 不进入浏览器、数据库或审计日志。结构化探针始终测试当前选中的模型。</p>
          <div className="integration-actions"><Button onClick={() => saveModel.mutate()} disabled={saveModel.isPending}>{saveModel.isPending ? '保存中...' : '保存策略与中转设置'}</Button><Button variant="secondary" icon={<Wifi size={15} />} onClick={() => probeModel.mutate()} disabled={probeModel.isPending}>{probeModel.isPending ? '探针运行中...' : '测试当前模型'}</Button></div>
          {saveModel.error ? <div className="inline-error" role="alert">{saveModel.error.message}</div> : null}
          {saveModel.isSuccess ? <div className="success-note" role="status">策略与中转设置已保存</div> : null}
          {probeModel.error ? <div className="inline-error" role="alert">{probeModel.error.message}</div> : null}
          {probeModel.isSuccess ? <div className="success-note" role="status">{probeModel.data?.detail ?? '结构化探针完成'}</div> : null}
        </div>
      </div>
    </section>
  </>
}

function formatUptime(seconds: number) { const hours = Math.floor(seconds / 3600); const minutes = Math.floor((seconds % 3600) / 60); return `${hours} 小时 ${minutes} 分钟` }

function StatusIcon({ healthy }: { healthy: boolean }) { return healthy ? <CheckCircle2 className="positive" size={17} aria-label="正常" /> : <XCircle className="warning" size={17} aria-label="未配置或异常" /> }
