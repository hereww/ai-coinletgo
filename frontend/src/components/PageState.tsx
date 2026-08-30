import { AlertCircle, RefreshCw } from 'lucide-react'
import { Button } from './Button'

export function PageLoading() {
  return <div className="page-state"><RefreshCw className="spin" size={20} /><span>正在同步数据...</span></div>
}

export function PageError({ message, retry }: { message: string; retry: () => void }) {
  return <div className="page-state error" role="alert"><AlertCircle size={20} /><span>{message}</span><Button icon={<RefreshCw size={15} />} onClick={retry}>重试</Button></div>
}
