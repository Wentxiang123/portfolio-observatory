import { useState } from 'react'
import type { FormEvent } from 'react'
import { api } from './api'
import type { AuthState } from './api'

type Enrollment = { qr_data_url: string; manual_key: string; username: string; expires_in: number }
export function RecoveryCodes({ codes, onDone }: { codes: string[]; onDone: () => void }) {
  const [saved, setSaved] = useState(false)
  return <div className="recovery-display"><h2>保存一次性恢复代码</h2><p>只显示这一次。每枚代码只能使用一次，请离线保存到安全位置。不要将代码、二维码或绑定密钥放入 Git、聊天或公开截图。</p><div className="recovery-grid">{codes.map(code => <code key={code}>{code}</code>)}</div><p>忘记密码：Authenticator + 一枚恢复代码。手机丢失：密码 + 一枚恢复代码。两者都丢失：两枚不同的恢复代码。</p><label className="check-label"><input type="checkbox" checked={saved} onChange={e => setSaved(e.target.checked)} />我已将恢复代码离线保存</label><button type="button" className="primary" disabled={!saved} onClick={onDone}>保存完成，继续</button></div>
}

export function AuthPanel({ state, onSuccess, binding = false }: { state: AuthState | null; onSuccess: (s: AuthState) => void; binding?: boolean }) {
  const [mode, setMode] = useState(binding ? 'local' : 'login')
  const [name, setName] = useState(''), [password, setPassword] = useState(''), [code, setCode] = useState('')
  const [oldName, setOldName] = useState('owner'), [oldPassword, setOldPassword] = useState('')
  const [recovery, setRecovery] = useState(''), [second, setSecond] = useState(''), [newPassword, setNewPassword] = useState('')
  const [recoveryMethod, setRecoveryMethod] = useState('password')
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null), [codes, setCodes] = useState<string[]>([])
  const [busy, setBusy] = useState(false), [error, setError] = useState('')
  async function perform(fn: () => Promise<void>) { setBusy(true); setError(''); try { await fn() } catch (e) { setError(e instanceof Error ? e.message : '操作失败') } finally { setBusy(false) } }
  async function done() { setCodes([]); setEnrollment(null); setPassword(''); setOldPassword(''); setRecovery(''); setSecond(''); setNewPassword(''); onSuccess(await api<AuthState>('/auth')) }
  async function submit(e: FormEvent) {
    e.preventDefault()
    await perform(async () => {
      if (enrollment) {
        const result = await api<{ recovery_codes: string[] }>('/auth/enroll/confirm', { code })
        setEnrollment(null); setCode(''); setCodes(result.recovery_codes); setPassword(''); setOldPassword(''); setNewPassword('')
      } else if (mode === 'login') {
        await api('/auth/login', { username: name, password }); setPassword(''); setMode('totp')
      } else if (mode === 'totp') { await api('/auth/login/verify', { code }); setCode(''); await done() }
      else if (mode === 'recover') {
        const result = await api<Enrollment>('/auth/recover', { username: name, new_password: newPassword, password: recoveryMethod === 'password' ? password : '', code: recoveryMethod === 'totp' ? code : '', recovery_code: recovery, second_recovery_code: recoveryMethod === 'two' ? second : '' })
        setEnrollment(result); setPassword(''); setNewPassword(''); setCode(''); setRecovery(''); setSecond('')
      } else if (mode === 'choose-local') {
        await api('/auth/mode', { mode: 'local', password: oldPassword, legacy_username: oldName }); await done()
      } else {
        const result = await api<Enrollment>('/auth/enroll/start', { username: name, password, intent: mode === 'register' ? 'register' : mode, legacy_username: oldName, legacy_password: oldPassword })
        setEnrollment(result); setPassword(''); setOldPassword(''); setCode('')
      }
    })
  }
  const title = enrollment ? '绑定 Authenticator' : ({ login: '登录个人工作台', totp: '输入 Authenticator 验证码', register: '创建本地账户', legacy: '升级原账户认证', local: '为本地工作空间启用认证', recover: '安全恢复账户', 'choose-local': '选择 Local Mode' } as Record<string, string>)[mode]
  const contents = codes.length ? <RecoveryCodes codes={codes} onDone={() => void perform(done)} /> : <>
    <span className="eyebrow">LOCAL IDENTITY / NO CLOUD REQUIRED</span><h2>{title}</h2>
    {enrollment ? <><p>使用 Microsoft Authenticator 或 Google Authenticator 扫描二维码，选择“其他账户”。输入应用显示的 6 位动态码后才会启用保护。绑定页 10 分钟内有效。</p><img className="totp-qr" src={enrollment.qr_data_url} alt="Authenticator 绑定二维码，请勿分享" /><details><summary>无法扫码？手工输入密钥</summary><code className="manual-key">{enrollment.manual_key}</code><p>账户：{enrollment.username}；类型：基于时间；6 位、30 秒、SHA-1。</p></details><label>Authenticator 验证码<input value={code} onChange={e => setCode(e.target.value)} autoComplete="one-time-code" inputMode="numeric" pattern="[0-9]{6}" maxLength={6} required /></label></> : <>
      {mode === 'choose-local' ? <><p>Local Mode 只允许本机访问，使用操作系统账户作为信任边界，不再要求网页密码或动态码。能够使用这台电脑的人也能查看投资数据。可在设置中重新启用账户认证。</p>{state?.legacy_available && <><label>原用户名<input value={oldName} onChange={e => setOldName(e.target.value)} required /></label><label>原本地密码<input type="password" value={oldPassword} onChange={e => setOldPassword(e.target.value)} autoComplete="current-password" required /></label></>}</> : <>
        {mode !== 'totp' && <label>{mode === 'legacy' ? '新登录用户名' : '用户名'}<input value={name} onChange={e => setName(e.target.value)} autoComplete="username" minLength={2} maxLength={40} required /></label>}
        {mode === 'legacy' && <><p>验证原密码后沿用原持仓。未绑定过邮箱的原账户用户名填写 owner；已绑定邮箱的填写原用户名。</p><label>原用户名<input value={oldName} onChange={e => setOldName(e.target.value)} required /></label><label>原本地密码<input type="password" value={oldPassword} onChange={e => setOldPassword(e.target.value)} autoComplete="current-password" required /></label></>}
        {['register', 'local'].includes(mode) && <p>{binding ? '完成绑定后切换到账户模式，并保留当前持仓。' : '用户名、密码与 Authenticator 全部在本机验证，不需要邮件或短信服务。已有数据请使用原账户升级入口。'}</p>}
        {mode === 'recover' && <><p>恢复将撤销全部旧会话，要求绑定新的 Authenticator 并生成新恢复代码。仅凭用户名无法恢复。</p><label>你还持有哪些凭据？<select value={recoveryMethod} onChange={e => { setRecoveryMethod(e.target.value); setPassword(''); setCode(''); setSecond('') }}><option value="password">记得密码，手机 / Authenticator 丢失</option><option value="totp">有 Authenticator，忘记密码</option><option value="two">密码和 Authenticator 都丢失，有两枚恢复代码</option></select></label><label>一次性恢复代码<input value={recovery} onChange={e => setRecovery(e.target.value)} autoComplete="off" maxLength={100} required /></label>{recoveryMethod === 'two' && <label>另一枚未使用的恢复代码<input value={second} onChange={e => setSecond(e.target.value)} autoComplete="off" required maxLength={100} /></label>}<label>新密码（至少 12 位）<input type="password" value={newPassword} onChange={e => setNewPassword(e.target.value)} autoComplete="new-password" minLength={12} maxLength={200} required /></label></>}
        {(mode === 'login' || ['register', 'legacy', 'local'].includes(mode) || (mode === 'recover' && recoveryMethod === 'password')) && <label>{['register', 'legacy', 'local'].includes(mode) ? '设置密码（至少 12 位）' : '密码'}<input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete={mode === 'login' || mode === 'recover' ? 'current-password' : 'new-password'} minLength={['register', 'legacy', 'local'].includes(mode) ? 12 : 1} maxLength={200} required /></label>}
        {(mode === 'totp' || (mode === 'recover' && recoveryMethod === 'totp')) && <label>Authenticator 验证码<input value={code} onChange={e => setCode(e.target.value)} autoComplete="one-time-code" inputMode="numeric" pattern="[0-9]{6}" maxLength={6} required /></label>}
        {mode === 'totp' && <p>动态码每 30 秒更新。已经使用的动态码不能重用；刚完成绑定后，请等待下一组动态码再登录。</p>}
      </>}
    </>}
    {error && <p role="alert" className="error">{error}</p>}
    <button className="primary" disabled={busy || !state}>{busy ? '正在验证…' : enrollment ? '验证并启用二步认证' : mode === 'login' ? '验证密码，继续' : mode === 'totp' ? '登录工作台' : mode === 'recover' ? '验证并重新绑定' : mode === 'choose-local' ? '确认使用 Local Mode' : '继续绑定 Authenticator'}</button>
    {!binding && <div className="auth-links">{[['login', '用户名登录'], ['register', '注册'], ['recover', '账户恢复'], ...(state?.legacy_available ? [['legacy', '原账户升级']] : []), ...(!state?.configured ? [['choose-local', '选择 Local Mode']] : [])].filter(([k]) => k !== mode).map(([k, label]) => <button type="button" key={k} disabled={busy} onClick={() => { setMode(k); setEnrollment(null); setPassword(''); setOldPassword(''); setCode(''); setRecovery(''); setSecond(''); setError('') }}>{label}</button>)}</div>}
    <small>v0.4 · 本地身份认证 · SMTP 配置保留但未启用</small>
  </>
  const form = <form className={binding ? 'binding-form' : 'auth-form'} onSubmit={submit}>{contents}</form>
  return binding ? <section className="card">{form}</section> : <div className="auth-page"><div className="auth-intro"><span className="logo-mark">P<span>.</span></span><span className="eyebrow">PERSONAL PORTFOLIO / 04</span><h1>你的投资，<br />你的本地工作台。</h1><p>用户名、密码和 Authenticator。<br />身份认证不依赖云端服务。</p><div className="auth-platforms">支付宝 · moomoo · OKX</div></div>{form}</div>
}

export function SecuritySettings({ state, onChanged }: { state: AuthState; onChanged: (s: AuthState) => void }) {
  const [password, setPassword] = useState(''), [code, setCode] = useState(''), [message, setMessage] = useState('')
  const [codes, setCodes] = useState<string[]>([]), [busy, setBusy] = useState(false)
  async function action(path: string, body: object) { setBusy(true); setMessage(''); try { const r = await api<{ recovery_codes?: string[] }>(path, body); setPassword(''); setCode(''); if (r.recovery_codes) setCodes(r.recovery_codes); else onChanged(await api<AuthState>('/auth')) } catch (e) { setMessage(e instanceof Error ? e.message : '操作失败') } finally { setBusy(false) } }
  if (state.mode === 'local' && !state.totp_enabled) return <AuthPanel binding state={state} onSuccess={onChanged} />
  return <section className="card"><h2>{state.mode === 'local' ? 'Local Mode · 当前免网页登录' : '账户安全 · 两步认证已启用'}</h2><p>{state.username} · {state.recovery_remaining} 枚恢复代码可用</p><p className="muted">修改安全设置需要密码和一枚未使用的动态码。切换模式会撤销旧会话。Local Mode 的访问权限由本机操作系统账户保护。</p>{codes.length ? <RecoveryCodes codes={codes} onDone={() => { setCodes([]); void api<AuthState>('/auth').then(onChanged) }} /> : <><div className="mapping-grid"><label>当前密码<input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" /></label><label>未使用的 Authenticator 验证码<input value={code} onChange={e => setCode(e.target.value)} inputMode="numeric" maxLength={6} autoComplete="one-time-code" /></label></div><div className="security-actions">{state.mode === 'account' && <button className="secondary" disabled={busy || !password || code.length !== 6} onClick={() => action('/auth/recovery-codes/rotate', { password, code })}>作废旧恢复代码并生成新代码</button>}<button className="secondary" disabled={busy || !password || code.length !== 6} onClick={() => action('/auth/mode', { mode: state.mode === 'local' ? 'account' : 'local', password, code })}>{state.mode === 'local' ? '切换到账户认证模式' : '关闭网页认证，切换为 Local Mode'}</button></div></>}{message && <p role="alert" className="error">{message}</p>}</section>
}
