import { useEffect, useState, type FormEvent, type InputHTMLAttributes } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Save, ShieldCheck } from 'lucide-react'
import { api } from '../api/client'
import type { RiskConfig } from '../api/types'
import { Button } from '../components/Button'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { PageHeader } from '../components/PageHeader'
import { PageError, PageLoading } from '../components/PageState'

export default function RiskPage() {
  const queryClient = useQueryClient()
  const config = useQuery({ queryKey: ['config'], queryFn: api.config })
  const [form, setForm] = useState<Partial<RiskConfig>>({})
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [customSymbols, setCustomSymbols] = useState('')
  const [validationError, setValidationError] = useState<string | null>(null)

  useEffect(() => {
    if (config.data) {
      setForm(config.data)
      setCustomSymbols((config.data.entry_symbols ?? []).filter((symbol) => !COMMON_ENTRY_SYMBOLS.includes(symbol)).join(', '))
    }
  }, [config.data])

  const save = useMutation({
    mutationFn: (password: string) => api.updateConfig({ ...form, password }),
    onSuccess: (data) => {
      queryClient.setQueryData(['config'], data)
      setConfirmOpen(false)
    },
  })

  if (config.isLoading) {
    return <><PageHeader title="风控" subtitle="开仓策略与确定性硬限制" /><PageLoading /></>
  }
  if (config.isError || !config.data) {
    return <><PageHeader title="风控" subtitle="开仓策略与确定性硬限制" /><PageError message={config.error?.message ?? '无法加载风控配置'} retry={() => config.refetch()} /></>
  }

  const update = (key: keyof RiskConfig, value: number | string | string[] | boolean) => {
    setValidationError(null)
    setForm((current) => ({ ...current, [key]: value }))
  }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    const positiveFields: Array<[string, number | undefined]> = [
      ['资金上限', form.capital_limit_usdt],
      ['单笔风险', form.single_trade_risk_pct],
      ['组合风险', form.portfolio_risk_pct],
      ['日亏损熔断', form.daily_loss_pct],
      ['总回撤熔断', form.max_drawdown_pct],
      ['保证金上限', form.max_margin_pct],
      ['最低净盈亏比', form.min_net_reward_risk],
      ['最小止损距离', form.min_stop_atr],
      ['最大止损距离', form.max_stop_atr],
    ]
    const invalidPositive = positiveFields.find(([, value]) => value === undefined || !Number.isFinite(value) || value <= 0)
    if (invalidPositive) {
      setValidationError(`${invalidPositive[0]}必须大于 0`)
      return
    }
    if (form.min_stop_atr !== undefined && form.max_stop_atr !== undefined && form.min_stop_atr > form.max_stop_atr) {
      setValidationError('最小止损距离不能大于最大止损距离')
      return
    }
    if (form.correlation_limit !== undefined && (form.correlation_limit < 0 || form.correlation_limit > 1)) {
      setValidationError('相关性阈值必须在 0 到 1 之间')
      return
    }
    if (form.min_confidence !== undefined && (form.min_confidence < 0 || form.min_confidence > 1)) {
      setValidationError('最低置信度必须在 0% 到 100% 之间')
      return
    }
    if ((form.max_positions ?? 0) < 1 || (form.max_same_direction ?? 0) < 1 || (form.candidate_count ?? 0) < 1) {
      setValidationError('仓位数量和候选合约数量必须至少为 1')
      return
    }
    if ((form.max_leverage ?? 0) < 1 || (form.max_leverage ?? 0) > 30) {
      setValidationError('最高杠杆必须在 1 到 30 倍之间')
      return
    }
    if ((form.scan_interval_minutes ?? 0) < 15 || (form.scan_interval_minutes ?? 0) > 120) {
      setValidationError('扫描周期必须在 15 到 120 分钟之间')
      return
    }
    setValidationError(null)
    setConfirmOpen(true)
  }
  const selectedSymbols = form.entry_symbols ?? []
  const toggleSymbol = (symbol: string) => {
    const next = selectedSymbols.includes(symbol)
      ? selectedSymbols.filter((item) => item !== symbol)
      : [...selectedSymbols, symbol]
    update('entry_symbols', next)
  }
  const updateCustomSymbols = (value: string) => {
    setCustomSymbols(value)
    const custom = value.split(',').map((item) => item.trim().toUpperCase()).filter(Boolean)
    const common = selectedSymbols.filter((symbol) => COMMON_ENTRY_SYMBOLS.includes(symbol))
    update('entry_symbols', [...new Set([...common, ...custom])])
  }

  return <>
    <PageHeader title="风控" subtitle="开仓策略与确定性硬限制" />
    <section className="risk-page-grid">
      <form className="surface settings-form" onSubmit={submit}>
        <div className="section-head"><h2>开仓策略</h2><ShieldCheck size={18} /></div>
        <p className="setup-note">这些参数会同时约束候选筛选、AI 开仓信号和最终硬风控。代币留空表示自动筛选全部符合条件的合约。</p>
        <div className="section-head"><h3>允许开仓代币</h3><span>{selectedSymbols.length ? `已选 ${selectedSymbols.length} 个` : '未限定'}</span></div>
        <div className="settings-grid">
          {COMMON_ENTRY_SYMBOLS.map((symbol) => <label className="checkbox-row" key={symbol}><input type="checkbox" checked={selectedSymbols.includes(symbol)} onChange={() => toggleSymbol(symbol)} /><span>{symbol}</span></label>)}
        </div>
        <label className="field-label">自定义代币（逗号分隔）<input value={customSymbols} onChange={(event) => updateCustomSymbols(event.target.value)} placeholder="例如 BTCUSDT, ETHUSDT" /></label>
        <div className="settings-grid">
          <SelectField label="允许开仓方向" value={form.entry_direction ?? 'both'} onChange={(value) => update('entry_direction', value)} options={[['both', '多空双向'], ['long_only', '仅做多'], ['short_only', '仅做空']]} />
          <SelectField label="入场触发" value={form.entry_trigger ?? 'breakout_or_pullback'} onChange={(value) => update('entry_trigger', value)} options={[['breakout_or_pullback', '突破或回踩'], ['breakout_only', '仅突破'], ['pullback_only', '仅回踩']]} />
          <NumberField label="候选合约数量" value={form.candidate_count ?? 5} step={1} onChange={(value) => update('candidate_count', value)} />
          <NumberField label="扫描周期（分钟）" value={form.scan_interval_minutes ?? 15} step={5} min={15} max={120} onChange={(value) => update('scan_interval_minutes', value)} />
          <NumberField label="最低置信度 (%)" value={(form.min_confidence ?? 0.75) * 100} step={1} onChange={(value) => update('min_confidence', value / 100)} />
          <NumberField label="最低净盈亏比" value={form.min_net_reward_risk ?? 2} step={0.1} onChange={(value) => update('min_net_reward_risk', value)} />
          <NumberField label="最小止损距离 (ATR)" value={form.min_stop_atr ?? 0.8} step={0.1} onChange={(value) => update('min_stop_atr', value)} />
          <NumberField label="最大止损距离 (ATR)" value={form.max_stop_atr ?? 2.5} step={0.1} onChange={(value) => update('max_stop_atr', value)} />
        </div>
        <p className="setup-note">扫描周期可设为 15–120 分钟，并按固定时间边界运行；15 分钟约为每天 96 次分析。受每日 110 次模型调用上限约束，不允许低于 15 分钟。保存后 Worker 会在等待期间自动重新计算下一次扫描时间。</p>

        <div className="section-head"><h2>账户级限制</h2><ShieldCheck size={18} /></div>
        <div className="settings-grid">
          <NumberField label="资金上限 (USDT)" value={form.capital_limit_usdt} step={100} onChange={(value) => update('capital_limit_usdt', value)} />
          <NumberField label="最高杠杆" value={form.max_leverage} step={1} min={1} max={30} onChange={(value) => update('max_leverage', value)} />
          <NumberField label="单笔风险 (%)" value={(form.single_trade_risk_pct ?? 0) * 100} step={0.05} onChange={(value) => update('single_trade_risk_pct', value / 100)} />
          <NumberField label="组合风险 (%)" value={(form.portfolio_risk_pct ?? 0) * 100} step={0.05} onChange={(value) => update('portfolio_risk_pct', value / 100)} />
          <NumberField label="日亏损熔断 (%)" value={(form.daily_loss_pct ?? 0) * 100} step={0.1} onChange={(value) => update('daily_loss_pct', value / 100)} />
          <NumberField label="总回撤熔断 (%)" value={(form.max_drawdown_pct ?? 0) * 100} step={0.5} onChange={(value) => update('max_drawdown_pct', value / 100)} />
          <NumberField label="保证金上限 (%)" value={(form.max_margin_pct ?? 0) * 100} step={1} onChange={(value) => update('max_margin_pct', value / 100)} />
          <NumberField label="最多仓位" value={form.max_positions} step={1} onChange={(value) => update('max_positions', value)} />
          <NumberField label="同向最多仓位" value={form.max_same_direction} step={1} onChange={(value) => update('max_same_direction', value)} />
          <NumberField label="相关性阈值 (0–1)" value={form.correlation_limit ?? 0.8} step={0.01} min={0} max={1} onChange={(value) => update('correlation_limit', value)} />
        </div>
        <p className="setup-note">杠杆可在 1–30× 之间调整。提高杠杆只降低所需保证金，单笔风险、组合风险、止损和保证金占用上限仍会独立限制仓位数量。</p>

        <div className="section-head"><h2>Portfolio-v1 组合调仓</h2><ShieldCheck size={18} /></div>
        <div className="settings-grid">
          <label className="checkbox-row"><input type="checkbox" checked={form.portfolio_strategy_enabled ?? false} onChange={(event) => update('portfolio_strategy_enabled', event.target.checked)} /><span>启用组合决策策略（仅测试网）</span></label>
          <NumberField label="调仓死区（风险份额）" value={(form.portfolio_rebalance_deadband_fraction ?? 0.1) * 100} step={1} min={0} max={100} onChange={(value) => update('portfolio_rebalance_deadband_fraction', value / 100)} />
          <NumberField label="调仓冷却（分钟）" value={form.portfolio_rebalance_cooldown_minutes ?? 30} step={5} min={0} max={1440} onChange={(value) => update('portfolio_rebalance_cooldown_minutes', value)} />
        </div>
        <p className="setup-note">Portfolio-v1 让 AI 输出组合风险预算和各币风险份额；数量、杠杆和订单细节仍由确定性风控计算。启用后只允许测试网执行，模型无效时不会产生调仓订单。</p>

        {validationError ? <div className="inline-error" role="alert">{validationError}</div> : null}
        {save.error ? <div className="inline-error" role="alert">{save.error.message}</div> : null}
        {save.isSuccess ? <div className="success-note">开仓与风控配置已更新并写入审计日志</div> : null}
        <Button type="submit" variant="primary" icon={<Save size={15} />} disabled={save.isPending}>{save.isPending ? '保存中...' : '保存配置'}</Button>
      </form>

      <aside className="surface immutable-limits">
        <div className="section-head"><h2>不可放宽规则</h2></div>
        <dl><div><dt>杠杆</dt><dd>1–30×</dd></div><div><dt>扫描周期</dt><dd>15–120 分钟</dd></div><div><dt>风险与止损数值</dt><dd>必须大于 0</dd></div><div><dt>止损区间</dt><dd>最小值 ≤ 最大值</dd></div><div><dt>置信度 / 相关性</dt><dd>0–1</dd></div><div><dt>补仓与马丁</dt><dd>禁止</dd></div></dl>
        <p className="setup-note">AI 策略模板（趋势跟随、平衡、保守、短线）在“设置 → AI 中转 / Responses API”中选择。</p>
      </aside>
    </section>
    <ConfirmDialog
      open={confirmOpen}
      title="确认保存开仓策略"
      body="保存后，Worker 会自动读取新的扫描周期；下一轮行情分析将按照新的方向、触发条件、代币范围和风控门槛运行。"
      confirmLabel="确认保存"
      requirePassword
      busy={save.isPending}
      error={save.error?.message}
      onClose={() => { if (!save.isPending) setConfirmOpen(false) }}
      onConfirm={(password) => save.mutate(password)}
    />
  </>
}

const COMMON_ENTRY_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT', 'ADAUSDT', 'LINKUSDT', 'AVAXUSDT', 'LTCUSDT']

function NumberField({ label, value, onChange, ...props }: { label: string; value?: number; onChange: (value: number) => void } & Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange'>) {
  return <label className="field-label">{label}<input type="number" value={value ?? ''} onChange={(event) => onChange(Number(event.target.value))} {...props} /></label>
}

function SelectField({ label, value, onChange, options }: { label: string; value?: string; onChange: (value: string) => void; options: Array<[string, string]> }) {
  return <label className="field-label">{label}<select value={value ?? ''} onChange={(event) => onChange(event.target.value)}>{options.map(([option, text]) => <option key={option} value={option}>{text}</option>)}</select></label>
}
