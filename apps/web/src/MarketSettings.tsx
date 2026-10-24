import { useEffect, useState } from 'react'
import { api } from './api'

type Instrument = { id: string; platform: string; symbol: string; currency: string; asset_type: string; market: string; provider_symbol: string; enabled: number; error: string | null; checked_at: string | null; next_poll: string; price: string | null; as_of: string | null; market_state: string; freshness: string; delay_label?: string }
const markets = { US: '美国 NYSE / Nasdaq', HK: '香港', CN_SH: '中国上海', CN_SZ: '中国深圳', JP: '日本东京', GB: '英国伦敦', CRYPTO: '加密货币 7×24', FUND_CN: '支付宝基金日终净值' }

function InstrumentEditor({ item, onSaved }: { item: Instrument; onSaved: () => Promise<void> }) {
  const [draft, setDraft] = useState(item), [busy, setBusy] = useState(false), [message, setMessage] = useState('')
  return <form className="instrument-form" onSubmit={async e => {
    e.preventDefault(); setBusy(true); setMessage('')
    try {
      await api(`/instruments/${item.id}`, { asset_type: draft.asset_type, market: draft.market, provider_symbol: draft.provider_symbol, enabled: !!draft.enabled }, 'PUT')
      setMessage('已保存，后台将按新配置更新价格。'); await onSaved()
    } catch (err) { setMessage(err instanceof Error ? err.message : '保存失败') } finally { setBusy(false) }
  }}>
    <div className="section-heading"><h3>{item.symbol} <small>{item.platform === 'Alipay' ? '支付宝' : item.platform} · {item.currency}</small></h3><span className="pill">{item.market_state}</span></div>
    <p className="muted">{item.price ? `${Number(item.price).toLocaleString('zh-CN', { maximumFractionDigits: 8 })} ${item.currency} · ${item.freshness}` : '等待获取价格'}{item.delay_label && ` · ${item.delay_label}`}</p>
    <div className="mapping-grid"><label>资产类型<select value={draft.asset_type} onChange={e => setDraft({ ...draft, asset_type: e.target.value })}><option value="stock">股票</option><option value="etf">场内 ETF</option><option value="fund">基金单位净值</option><option value="crypto">加密现货</option></select></label>
      <label>交易市场<select value={draft.market} onChange={e => setDraft({ ...draft, market: e.target.value })}><option value="">请选择市场</option>{Object.entries(markets).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
      <label>行情代码<input value={draft.provider_symbol} onChange={e => setDraft({ ...draft, provider_symbol: e.target.value.toUpperCase() })} required /></label>
      <label>自动更新<select value={draft.enabled} onChange={e => setDraft({ ...draft, enabled: Number(e.target.value) })}><option value={1}>开启</option><option value={0}>暂停</option></select></label>
    </div><div className="section-heading"><small>最近检查：{item.checked_at ? new Date(item.checked_at).toLocaleString('zh-CN') : '尚未检查'}{item.as_of && `；价格时间：${new Date(item.as_of).toLocaleString('zh-CN')}`}</small><button className="secondary" disabled={busy}>保存行情关联</button></div>
    {item.error && <p className="error">{item.error}</p>}{message && <p role="status">{message}</p>}
  </form>
}

export function MarketSettings() {
  const [items, setItems] = useState<Instrument[]>([]), [message, setMessage] = useState(''), [heartbeat, setHeartbeat] = useState(false)
  async function load() {
    const [data, state] = await Promise.all([api<Instrument[]>('/instruments'), api<{ running: boolean }>('/quotes/status')])
    setItems(data); setHeartbeat(state.running)
  }
  useEffect(() => { void Promise.resolve().then(load).catch(e => setMessage(e.message)); const timer = setInterval(() => void load().catch(e => setMessage(e.message)), 15000); return () => clearInterval(timer) }, [])
  return <section className="card"><div className="section-heading"><h2>自动行情与资产关联</h2><span className="pill">{heartbeat ? '行情 Worker 运行中' : '行情 Worker 未运行或无心跳'}</span></div>
    <p className="muted">加密货币全天更新；股票和场内 ETF 按所属市场常规交易时段更新。支付宝基金只显示已公布日终单位净值，当天未公布时保留上一期，QDII 可能更晚。</p>
    <p className="muted">免费股票行情延迟未知，不保证实时。代码示例：AAPL、0700.HK、600519.SS、000001.SZ、7203.T、SOL-USDT；支付宝基金填六位基金代码。自动关联依据来源平台，请核对市场与代码。</p>
    <button className="secondary" onClick={async () => { try { const r = await api<{ message: string }>('/quotes/refresh', {}); setMessage(r.message); await load() } catch (e) { setMessage(e instanceof Error ? e.message : '更新失败') } }}>发现持仓并请求更新</button>
    {message && <p role="status">{message}</p>}{!items.length && <p className="muted">导入持仓后，Worker 会自动发现资产。也可以点击上方按钮立即建立关联。</p>}
    {items.map(i => <InstrumentEditor key={i.id} item={i} onSaved={load} />)}
  </section>
}
