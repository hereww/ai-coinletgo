import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { AlertTriangle, X } from 'lucide-react'
import { Button } from './Button'

interface ConfirmDialogProps {
  open: boolean
  title: string
  body: string
  confirmLabel: string
  requirePassword?: boolean
  danger?: boolean
  busy?: boolean
  error?: string
  children?: ReactNode
  onClose: () => void
  onConfirm: (password: string) => void
}

export function ConfirmDialog(props: ConfirmDialogProps) {
  const [password, setPassword] = useState('')
  const dialogRef = useRef<HTMLElement>(null)
  const initialFocusRef = useRef<HTMLInputElement>(null)
  const previousFocusRef = useRef<HTMLElement | null>(null)
  const titleId = useId()
  const bodyId = useId()

  useEffect(() => {
    if (!props.open) return
    setPassword('')
    previousFocusRef.current = document.activeElement as HTMLElement | null
    const focusTarget = initialFocusRef.current ?? dialogRef.current
    focusTarget?.focus()
    return () => previousFocusRef.current?.focus()
  }, [props.open])

  if (!props.open) return null
  const canConfirm = !props.requirePassword || password.length > 0

  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape' && !props.busy) {
      event.preventDefault()
      props.onClose()
      return
    }
    if (event.key !== 'Tab' || !dialogRef.current) return
    const focusable = Array.from(
      dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    )
    if (!focusable.length) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && !props.busy && props.onClose()}>
      <section ref={dialogRef} className="dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={bodyId} aria-busy={props.busy} tabIndex={-1} onKeyDown={handleKeyDown} onMouseDown={(event) => event.stopPropagation()}>
        <div className="dialog-head">
          <div className={`dialog-icon ${props.danger ? 'danger' : ''}`}><AlertTriangle size={19} /></div>
          <h2 id={titleId}>{props.title}</h2>
          <button type="button" className="icon-button" onClick={props.onClose} aria-label="关闭" disabled={props.busy}><X size={18} /></button>
        </div>
        <p id={bodyId}>{props.body}</p>
        {props.children}
        {props.requirePassword ? (
          <label className="field-label">操作密码
            <input ref={initialFocusRef} type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" />
          </label>
        ) : null}
        {props.error ? <div className="inline-error" role="alert" aria-live="assertive">{props.error}</div> : null}
        <div className="dialog-actions">
          <Button type="button" onClick={props.onClose} disabled={props.busy}>取消</Button>
          <Button type="button" variant={props.danger ? 'danger' : 'primary'} onClick={() => props.onConfirm(password)} disabled={!canConfirm || props.busy}>{props.busy ? '处理中...' : props.confirmLabel}</Button>
        </div>
      </section>
    </div>
  )
}
