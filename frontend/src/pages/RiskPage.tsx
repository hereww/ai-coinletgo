import { useEffect, useState, type FormEvent, type InputHTMLAttributes } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Save, ShieldCheck } from 'lucide-react'
import { api } from '../api/client'
import type { RiskConfig } from '../api/types'
import { Button } from '../components/Button'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { PageHeader } from '../components/PageHeader'
import { PageError, PageLoading } from '../components/PageState'

const TESTNET_ENTRY_DEFAULTS: Partial<RiskConfig> = {
  model_strategy_enabled: true,
  manual_exit_levels_enabled: true,
  manual_stop_atr: 1.8,
  manual_take_profit_atr: 5,
  model_primary_portfolio_enabled: false,
  strong_trend_entry_override_enabled: false,
  strong_trend_adx_min: 30,
}

export default function RiskPage() {
  const queryClient = useQueryClient()
  const config = useQuery({ queryKey: ['config'], queryFn: api.config })
  const [form, setForm] = useState<Partial<RiskConfig>>({})
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [customSymbols, setCustomSymbols] = useState('')
  const [hftSymbols, setHftSymbols] = useState('')
  const [validationError, setValidationError] = useState<string | null>(null)

  useEffect(() => {
    if (config.data) {
      setForm({
        ...TESTNET_ENTRY_DEFAULTS,
        ...config.data,
      })
      setCustomSymbols((config.data.entry_symbols ?? []).filter((symbol) => !COMMON_ENTRY_SYMBOLS.includes(symbol)).join(', '))
      setHftSymbols((config.data.hft_symbols ?? []).join(', '))
    }
  }, [config.data])

  const save = useMutation({
    mutationFn: (password: string) => api.updateConfig({ ...form, password }),
    onSuccess: (data) => {
      queryClient.setQueryData(['config'], data)
      void queryClient.invalidateQueries({ queryKey: ['dashboard'] })
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
      ['手动止损距离', form.manual_stop_atr],
      ['手动止盈距离', form.manual_take_profit_atr],
      ['高波动风险系数', form.elevated_volatility_risk_multiplier ?? 0.75],
      ['极端波动风险系数', form.high_volatility_risk_multiplier ?? 0.5],
      ['HFT 最小盘口深度', form.hft_min_depth_usdt],
      ['HFT 单笔名义价值', form.hft_order_notional_usdt],
      ['HFT 最大库存', form.hft_max_inventory_usdt],
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
    if (form.manual_exit_levels_enabled && form.manual_stop_atr !== undefined && form.max_stop_atr !== undefined && form.manual_stop_atr > form.max_stop_atr) {
      setValidationError('手动止损距离不能大于最大止损距离')
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
    if (form.strong_trend_adx_min !== undefined && (!Number.isFinite(form.strong_trend_adx_min) || form.strong_trend_adx_min < 0)) {
      setValidationError('强趋势最低 ADX 不能小于 0')
      return
    }
    if (
      form.volatility_soft_limit_percentile !== undefined
      && form.volatility_hard_limit_percentile !== undefined
      && form.volatility_soft_limit_percentile > form.volatility_hard_limit_percentile
    ) {
      setValidationError('高波动分位不能大于极端波动分位')
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
    if (![5, 15, 30, 60].includes(form.scan_interval_minutes ?? 0)) {
      setValidationError('扫描周期只能选择 5、15、30 或 60 分钟')
      return
    }
    if (form.hft_event_interval_ms !== undefined && (form.hft_event_interval_ms < 50 || form.hft_event_interval_ms > 5000)) {
      setValidationError('HFT 事件处理间隔必须在 50 到 5000 毫秒之间')
      return
    }
    if (form.hft_max_spread_pct !== undefined && (form.hft_max_spread_pct <= 0 || form.hft_max_spread_pct > 0.02)) {
      setValidationError('HFT 最大点差必须在 0% 到 2% 之间')
      return
    }
    if (form.hft_imbalance_threshold !== undefined && (form.hft_imbalance_threshold <= 0 || form.hft_imbalance_threshold >= 1)) {
      setValidationError('HFT 盘口不平衡阈值必须在 0% 到 100% 之间')
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
          <label className="checkbox-row"><input type="checkbox" checked={form.model_strategy_enabled ?? true} onChange={(event) => update('model_strategy_enabled', event.target.checked)} /><span>启用模型策略（关闭后使用本地规则）</span></label>
          {COMMON_ENTRY_SYMBOLS.map((symbol) => <label className="checkbox-row" key={symbol}><input type="checkbox" checked={selectedSymbols.includes(symbol)} onChange={() => toggleSymbol(symbol)} /><span>{symbol}</span></label>)}
        </div>
        <label className="field-label">自定义代币（逗号分隔）<input value={customSymbols} onChange={(event) => updateCustomSymbols(event.target.value)} placeholder="例如 BTCUSDT, ETHUSDT" /></label>
        <div className="settings-grid">
          <SelectField label="允许开仓方向" value={form.entry_direction ?? 'both'} onChange={(value) => update('entry_direction', value)} options={[['both', '多空双向'], ['long_only', '仅做多'], ['short_only', '仅做空']]} />
          <SelectField label="入场触发" value={form.entry_trigger ?? 'breakout_or_pullback'} onChange={(value) => update('entry_trigger', value)} options={[['breakout_or_pullback', '突破或回踩'], ['breakout_only', '仅突破'], ['pullback_only', '仅回踩']]} />
          <NumberField label="候选合约数量" value={form.candidate_count ?? 3} step={1} onChange={(value) => update('candidate_count', value)} />
          <SelectField label="扫描周期（分钟）" value={String(form.scan_interval_minutes ?? 5)} onChange={(value) => update('scan_interval_minutes', Number(value))} options={[['5', '5 分钟'], ['15', '15 分钟'], ['30', '30 分钟'], ['60', '60 分钟']]} />
          <NumberField label="最低置信度 (%)" value={(form.min_confidence ?? 0.75) * 100} step={1} onChange={(value) => update('min_confidence', value / 100)} />
          <NumberField label="最低净盈亏比" value={form.min_net_reward_risk ?? 2.5} step={0.1} min={1} onChange={(value) => update('min_net_reward_risk', value)} />
          <NumberField label="最小止损距离 (ATR)" value={form.min_stop_atr ?? 0.8} step={0.1} onChange={(value) => update('min_stop_atr', value)} />
          <NumberField label="最大止损距离 (ATR)" value={form.max_stop_atr ?? 4} step={0.1} onChange={(value) => update('max_stop_atr', value)} />
          <label className="checkbox-row"><input type="checkbox" checked={form.manual_exit_levels_enabled ?? true} onChange={(event) => update('manual_exit_levels_enabled', event.target.checked)} /><span>启用手动止盈止损（新开仓）</span></label>
          <NumberField label="手动止损距离 (ATR)" value={form.manual_stop_atr ?? 1.8} step={0.1} min={0.1} max={10} onChange={(value) => update('manual_stop_atr', value)} />
          <NumberField label="手动止盈距离 (ATR)" value={form.manual_take_profit_atr ?? 5} step={0.1} min={0.1} max={20} onChange={(value) => update('manual_take_profit_atr', value)} />
          <label className="checkbox-row"><input type="checkbox" checked={form.model_primary_portfolio_enabled ?? false} onChange={(event) => update('model_primary_portfolio_enabled', event.target.checked)} /><span>模型主导组合决策（测试网）</span></label>
          <label className="checkbox-row"><input type="checkbox" checked={form.strong_trend_entry_override_enabled ?? false} onChange={(event) => update('strong_trend_entry_override_enabled', event.target.checked)} /><span>强劲上升趋势策略放行（测试网）</span></label>
          <NumberField label="强趋势最低 ADX" value={form.strong_trend_adx_min ?? 30} step={1} min={0} onChange={(value) => update('strong_trend_adx_min', value)} />
          <NumberField label="最低趋势强度 (ADX)" value={form.trend_adx_min ?? 20} step={1} min={0} onChange={(value) => update('trend_adx_min', value)} />
          <NumberField label="高波动分位 (%)" value={(form.volatility_soft_limit_percentile ?? 0.75) * 100} step={1} min={0} max={100} onChange={(value) => update('volatility_soft_limit_percentile', value / 100)} />
          <NumberField label="极端波动分位 (%)" value={(form.volatility_hard_limit_percentile ?? 0.9) * 100} step={1} min={0} max={100} onChange={(value) => update('volatility_hard_limit_percentile', value / 100)} />
          <NumberField label="高波动风险系数" value={form.elevated_volatility_risk_multiplier ?? 0.75} step={0.05} min={0.05} max={1} onChange={(value) => update('elevated_volatility_risk_multiplier', value)} />
          <NumberField label="极端波动风险系数" value={form.high_volatility_risk_multiplier ?? 0.5} step={0.05} min={0.05} max={1} onChange={(value) => update('high_volatility_risk_multiplier', value)} />
        </div>
        <p className="setup-note">模型主导开启后，模型负责方向、机会和目标仓位；置信度、ADX、趋势和 15 分钟触发作为判断证据。最低净盈亏比、单笔与组合风险、同向仓位、相关性、调仓冷却、保证金、余额、交易所精度、系统模式和熔断始终由本地硬风控执行。</p>
        <p className="setup-note">关闭模型策略后，测试网不再调用远程模型，改由 rule-based-v1 按多周期趋势、ADX、流动性、资金费率、基差、波动率和成本后净盈亏比生成信号；只要 1 小时与 4 小时趋势一致，即使当前周期没有 15 分钟突破或回踩也可进入硬风控评估，全部止损、净盈亏比、仓位、相关性、保证金和保护单规则仍然生效。实盘必须保持开启。</p>
        <p className="setup-note">启用风控接管止盈止损后，新开仓按 ATR 自动生成止损/最终止盈（默认 1.8 / 5.0 ATR），覆盖模型绝对价；已有仓位沿用已生效的交易所保护单，不会被模型放宽或频繁改写。</p>
        <p className="setup-note">扫描周期保持固定时间边界运行；当前配置不在本次风控收紧范围内。保存后 Worker 会在等待期间自动重新计算下一次扫描时间。</p>

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
          <NumberField label="调仓死区（风险份额）" value={(form.portfolio_rebalance_deadband_fraction ?? 0.25) * 100} step={1} min={0} max={100} onChange={(value) => update('portfolio_rebalance_deadband_fraction', value / 100)} />
          <NumberField label="调仓冷却（分钟）" value={form.portfolio_rebalance_cooldown_minutes ?? 120} step={5} min={0} max={1440} onChange={(value) => update('portfolio_rebalance_cooldown_minutes', value)} />
        </div>
        <p className="setup-note">Portfolio-v1 让 AI 输出组合风险预算和各币风险份额；数量、杠杆和订单细节仍由确定性风控计算。启用后只允许测试网执行，模型无效时不会产生调仓订单。</p>

        <div className="section-head"><h2>因子策略控制</h2><ShieldCheck size={18} /></div>
        <div className="settings-grid">
          <label className="checkbox-row"><input type="checkbox" checked={form.historical_research_enabled ? (form.factor_policy_enabled ?? false) : false} disabled={!form.historical_research_enabled} onChange={(event) => update('factor_policy_enabled', event.target.checked)} /><span>启用 ACTIVE 因子排序与风险倍率（测试网）</span></label>
          <NumberField label="因子排名权重 (%)" value={(form.factor_rank_weight ?? 0.2) * 100} step={1} min={0} max={100} onChange={(value) => update('factor_rank_weight', value / 100)} />
          <NumberField label="因子最低风险倍率" value={form.factor_min_risk_multiplier ?? 0.75} step={0.05} min={0.05} max={1} onChange={(value) => update('factor_min_risk_multiplier', value)} />
          <NumberField label="自动升版成熟窗口" value={form.factor_promotion_windows ?? 30} step={1} min={1} max={365} onChange={(value) => update('factor_promotion_windows', value)} />
        </div>
        <p className="setup-note">{form.historical_research_enabled ? '关闭开关会立即让后续周期回退原排序和 1.0 因子倍率，不影响研究与影子窗口记录。因子不决定交易方向，也不直接否决入场。' : '历史研究总开关已关闭，因子排序、风险倍率和影子窗口均不参与当前自动交易。'}</p>

        <div className="section-head"><h2>HFT 盘口 shadow</h2><ShieldCheck size={18} /></div>
        <div className="settings-grid">
          <label className="checkbox-row"><input type="checkbox" checked={form.hft_enabled ?? false} onChange={(event) => update('hft_enabled', event.target.checked)} /><span>启用 HFT 盘口策略（仅测试网）</span></label>
          <label className="checkbox-row"><input type="checkbox" checked disabled /><span>dry-run 纸面成交（强制开启）</span></label>
          <label className="field-label">HFT 合约（逗号分隔）<input value={hftSymbols} onChange={(event) => { const value = event.target.value; setHftSymbols(value); update('hft_symbols', value.split(',').map((item) => item.trim().toUpperCase()).filter(Boolean)) }} placeholder="例如 BTCUSDT, ETHUSDT" /></label>
          <NumberField label="事件处理间隔 (ms)" value={form.hft_event_interval_ms ?? 100} step={50} min={50} max={5000} onChange={(value) => update('hft_event_interval_ms', value)} />
          <NumberField label="最大点差 (%)" value={(form.hft_max_spread_pct ?? 0.0008) * 100} step={0.01} min={0.01} max={2} onChange={(value) => update('hft_max_spread_pct', value / 100)} />
          <NumberField label="最小双边深度 (USDT)" value={form.hft_min_depth_usdt ?? 25000} step={1000} onChange={(value) => update('hft_min_depth_usdt', value)} />
          <NumberField label="单笔名义价值 (USDT)" value={form.hft_order_notional_usdt ?? 50} step={5} onChange={(value) => update('hft_order_notional_usdt', value)} />
          <NumberField label="最大库存 (USDT)" value={form.hft_max_inventory_usdt ?? 250} step={10} onChange={(value) => update('hft_max_inventory_usdt', value)} />
          <NumberField label="交易冷却 (秒)" value={form.hft_cooldown_seconds ?? 3} step={1} min={0} max={3600} onChange={(value) => update('hft_cooldown_seconds', value)} />
          <NumberField label="行情 stale 阈值 (秒)" value={form.hft_market_stale_seconds ?? 2} step={0.1} min={0.1} max={60} onChange={(value) => update('hft_market_stale_seconds', value)} />
          <NumberField label="连续亏损熔断次数" value={form.hft_max_consecutive_losses ?? 3} step={1} min={1} max={100} onChange={(value) => update('hft_max_consecutive_losses', value)} />
          <NumberField label="盘口不平衡阈值 (%)" value={(form.hft_imbalance_threshold ?? 0.2) * 100} step={1} min={1} max={99} onChange={(value) => update('hft_imbalance_threshold', value / 100)} />
        </div>
        <p className="setup-note">HFT 首版读取 Binance Futures `depth@100ms` 差分盘口，用盘口不平衡和 microprice 生成 shadow 信号；只做纸面成交，不调用真实下单接口。测试网、API 密钥和代理条件不满足时，HFT 不会启动。</p>

        {validationError ? <div className="inline-error" role="alert">{validationError}</div> : null}
        {save.error && !confirmOpen ? <div className="inline-error" role="alert">{save.error.message}</div> : null}
        {save.isSuccess ? <div className="success-note">开仓与风控配置已更新并写入审计日志</div> : null}
        <Button type="submit" variant="primary" icon={<Save size={15} />} disabled={save.isPending}>{save.isPending ? '保存中...' : '保存配置'}</Button>
      </form>

      <aside className="surface immutable-limits">
        <div className="section-head"><h2>不可放宽规则</h2></div>
        <dl><div><dt>杠杆</dt><dd>1–30×</dd></div><div><dt>扫描周期</dt><dd>5 / 15 / 30 / 60 分钟</dd></div><div><dt>风险与止损数值</dt><dd>必须大于 0</dd></div><div><dt>止损区间</dt><dd>最小值 ≤ 最大值</dd></div><div><dt>手动止盈止损</dt><dd>仅测试网新开仓</dd></div><div><dt>强趋势放行</dt><dd>不放宽任何组合硬限制</dd></div><div><dt>净盈亏比 / 相关性</dt><dd>OPEN 与 ADD 强制检查</dd></div><div><dt>因子执行</dt><dd>仅测试网 ACTIVE 版本</dd></div><div><dt>HFT 执行</dt><dd>仅测试网 dry-run</dd></div><div><dt>补仓与马丁</dt><dd>禁止</dd></div></dl>
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
