import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { AlertTriangle, X } from 'lucide-react'
import { Button } from './Button'

interface ConfirmDialogProps {
  open: boolean
  title: string
  body: string
  confirmLabel: string
  confirmationText?: string
  requireTotp?: boolean
  danger?: boolean
  busy?: boolean
  error?: string
  children?: ReactNode
  onClose: () => void
  onConfirm: (totp: string) => void
}

export function ConfirmDialog(props: ConfirmDialogProps) {
  const [totp, setTotp] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const dialogRef = useRef<HTMLElement>(null)
  const initialFocusRef = useRef<HTMLInputElement>(null)
  const previousFocusRef = useRef<HTMLElement | null>(null)
  const titleId = useId()
  const bodyId = useId()

  useEffect(() => {
    if (!props.open) return
    setTotp('')
    setConfirmation('')
    previousFocusRef.current = document.activeElement as HTMLElement | null
    const focusTarget = initialFocusRef.current ?? dialogRef.current
    focusTarget?.focus()
    return () => previousFocusRef.current?.focus()
  }, [props.open])

  if (!props.open) return null
  const canConfirm = (!props.requireTotp || totp.length === 6) && (!props.confirmationText || confirmation === props.confirmationText)

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
        {props.confirmationText ? (
          <label className="field-label">输入 {props.confirmationText} 确认
            <input ref={!props.requireTotp ? initialFocusRef : undefined} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" />
          </label>
        ) : null}
        {props.requireTotp ? (
          <label className="field-label">TOTP 验证码
            <input ref={initialFocusRef} inputMode="numeric" maxLength={6} value={totp} onChange={(event) => setTotp(event.target.value.replace(/\D/g, ''))} autoComplete="one-time-code" />
          </label>
        ) : null}
        {props.error ? <div className="inline-error" role="alert" aria-live="assertive">{props.error}</div> : null}
        <div className="dialog-actions">
          <Button type="button" onClick={props.onClose} disabled={props.busy}>取消</Button>
          <Button type="button" variant={props.danger ? 'danger' : 'primary'} onClick={() => props.onConfirm(totp)} disabled={!canConfirm || props.busy}>{props.busy ? '处理中...' : props.confirmLabel}</Button>
        </div>
      </section>
    </div>
  )
}
