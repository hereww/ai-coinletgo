import type { ButtonHTMLAttributes, ReactNode } from 'react'

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost'

export function Button({
  children,
  variant = 'secondary',
  icon,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; icon?: ReactNode }) {
  return (
    <button className={`button button-${variant}`} {...props}>
      {icon}
      <span>{children}</span>
    </button>
  )
}

