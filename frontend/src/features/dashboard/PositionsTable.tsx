import { ShieldCheck, XCircle } from 'lucide-react'
import type { Position } from '../../api/types'
import { Button } from '../../components/Button'

const amount = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export function PositionsTable({ positions, onReduce }: { positions: Position[]; onReduce?: (position: Position) => void }) {
  return (
    <div className="table-scroll">
      <table className="data-table positions-table">
        <thead><tr><th>合约</th><th>方向</th><th>数量</th><th>开仓均价</th><th>标记价格</th><th>未实现盈亏</th><th>止损</th><th>当前 R</th><th>保护</th><th aria-label="操作" /></tr></thead>
        <tbody>
          {positions.length === 0 ? <tr><td colSpan={10} className="empty-cell">当前没有持仓</td></tr> : positions.map((position) => {
            const pnl = Number(position.unrealized_pnl)
            return (
              <tr key={position.position_id}>
                <td><strong>{position.symbol}</strong></td>
                <td><span className={`side-text ${position.side === 'LONG' ? 'long' : 'short'}`}>{position.side === 'LONG' ? '多' : '空'}</span></td>
                <td className="mono">{position.quantity}</td>
                <td className="mono">{amount.format(Number(position.entry_price))}</td>
                <td className="mono">{amount.format(Number(position.mark_price))}</td>
                <td className={`mono ${pnl >= 0 ? 'positive' : 'negative'}`}>{pnl >= 0 ? '+' : ''}{amount.format(pnl)}</td>
                <td className="mono">{amount.format(Number(position.stop_price))}</td>
                <td className="mono">{Number(position.current_r).toFixed(2)}R</td>
                <td>{position.protected ? <span className="protection good"><ShieldCheck size={15} />已保护</span> : <span className="protection bad"><XCircle size={15} />异常</span>}</td>
                <td>{onReduce ? <Button variant="ghost" onClick={() => onReduce(position)}>减仓</Button> : null}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

