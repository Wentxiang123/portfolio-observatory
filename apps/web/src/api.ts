export async function api<T>(path: string, body?: unknown, method?: string): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: method ?? (body === undefined ? 'GET' : 'POST'), credentials: 'include',
    headers: body instanceof FormData || body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
  })
  const data = await response.json()
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith('/auth')) window.dispatchEvent(new Event('auth-expired'))
    throw new Error(typeof data.detail === 'string' ? data.detail : '请求未通过校验，请检查输入内容。')
  }
  return data
}

export type AuthState = {
  configured: boolean; logged_in: boolean; legacy_available?: boolean;
  mode: 'account' | 'local'; username?: string; totp_enabled: boolean; recovery_remaining: number;
}
