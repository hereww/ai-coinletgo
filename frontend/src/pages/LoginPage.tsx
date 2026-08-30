import { useState, type FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Activity, LockKeyhole } from 'lucide-react'
import { api, apiBaseUrl } from '../api/client'
import { Button } from '../components/Button'

export function LoginPage({ onSuccess }: { onSuccess: () => void }) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const login = useMutation({ mutationFn: () => api.login(username, password), onSuccess })
  const submit = (event: FormEvent) => { event.preventDefault(); login.mutate() }

  return (
    <main className="login-shell">
      <section className="login-panel">
        <div className="login-brand"><Activity size={21} /><span>合约风控台</span></div>
        <div className="login-heading"><LockKeyhole size={24} /><h1>操作员登录</h1></div>
        <div className="login-endpoint" role="status"><span>连接目标</span><code>{apiBaseUrl || '当前页面同源 API'}</code></div>
        <form onSubmit={submit}>
          <label className="field-label">用户名<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required /></label>
          <label className="field-label">密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required /></label>
          {login.error ? <div className="inline-error">{login.error.message}</div> : null}
          <Button variant="primary" type="submit" disabled={login.isPending}>{login.isPending ? '验证中...' : '登录控制台'}</Button>
        </form>
      </section>
    </main>
  )
}
