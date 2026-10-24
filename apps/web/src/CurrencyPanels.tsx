import type { Dashboard } from './App'

const number = (n: string | null, digits = 4) => n == null ? '—' : Number(n).toLocaleString('zh-CN', { maximumFractionDigits: digits })
const platform = (p?: string) => p === 'Alipay' ? '支付宝' : p
export function CurrencyPanels({ data, analytics = false }: { data: Dashboard; analytics?: boolean }) {
  return <>
    <div className="info-note">所有平台持仓已归入「我的投资账户」。以下按原币种汇总；尚未引入汇率，因此不将人民币、美元和 USDT 相加。</div>
    <div className="currency-grid">{data.groups?.map(g => <section className="card currency-card" key={g.currency}>
      <div className="section-heading"><div><span className="eyebrow">ALL PLATFORMS</span><h2>{g.currency} 资产</h2></div><span className="pill">{g.positions.length} 项持仓</span></div>
      <strong className="currency-value">{number(g.known_value)} <small>{g.currency}</small></strong>
      <p className="muted">{g.complete_valuation ? '所有持仓均有已存价格' : '仅已定价部分，尚有资产缺价'}</p>
      <dl><div><dt>现金</dt><dd>{number(g.cash)}</dd></div><div><dt>持仓成本</dt><dd>{number(g.cost)}</dd></div><div><dt>浮动盈亏</dt><dd className={Number(g.unrealized) >= 0 ? 'positive' : 'negative'}>{number(g.unrealized)}</dd></div><div><dt>已实现盈亏</dt><dd>{number(g.realized)}</dd></div></dl>
      <div className="source-tags">{[...new Set(g.positions.map(p => p.platform))].map(p => <span className="pill" key={p}>{platform(p)}</span>)}</div>
    </section>)}</div>
    {!analytics && <section className="card"><h2>全部平台持仓</h2><div className="table-scroll"><table><thead><tr><th>资产 / 平台</th><th>数量</th><th>单位成本</th><th>市值 / 币种</th><th>浮盈 / 浮亏</th><th>最新价格 / 行情状态</th></tr></thead><tbody>{data.positions.map(p => <tr key={`${p.source_account_id}:${p.symbol}:${p.currency}`}><td><b>{p.symbol}</b><small>{platform(p.platform)}</small></td><td>{number(p.quantity, 8)}</td><td>{number(p.average_cost, 6)}</td><td>{number(p.value)} {p.currency}</td><td>{number(p.pnl)} {p.currency}</td><td>{p.quote ? <>{number(p.quote.price, 6)} {p.currency}<small>{p.quote.nav_date ? `净值日期 ${p.quote.nav_date}` : new Date(p.quote.as_of).toLocaleString('zh-CN')}</small><small>{p.quote.delay_label} · {p.quote.freshness}</small>{p.quote.update_error && <small className="error">更新失败，保留旧价</small>}</> : '缺少价格'}</td></tr>)}</tbody></table></div></section>}
    {analytics && data.groups?.map(g => <section className="card" key={g.currency}><h2>{g.currency} · 盈利原因与情景分析</h2><div className="equation"><div><small>净持仓价格贡献</small><strong>{number(g.attribution.market_appreciation)}</strong></div><b>−</b><div><small>买入费用拖累</small><strong>{number(g.attribution.trade_fee_drag)}</strong></div><b>=</b><div><small>持仓浮盈 / 浮亏</small><strong>{number(g.unrealized)}</strong></div></div><p className="muted">“—”表示数据条件不满足。各来源独立核算成本，再按相同币种汇总；不同估值窗口的24小时变化不直接相加。</p><div className="scenario-grid">{g.scenarios.map(s => <div key={s.shock}><span>持仓同步 {s.shock > 0 ? '+' : ''}{s.shock}%</span><strong>{number(s.change)}</strong><small>资产 {number(s.nav)} {g.currency}</small></div>)}</div></section>)}
    <section className="card"><h2>账户范围与数据质量</h2><ul className="quality-list">{data.warnings.map(w => <li key={w}>{w}</li>)}</ul><p className="muted">最大回撤、年化收益、夏普与全账户集中度仍需完整净值、资产标识及汇率数据。</p></section>
  </>
}
