import { Inbox } from 'lucide-react'

export function EmptyState({ title, detail }: { title: string; detail: string }) {
  return <div className="empty-state"><Inbox size={23} /><strong>{title}</strong><span>{detail}</span></div>
}

