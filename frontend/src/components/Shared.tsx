import { AlertCircle, ArrowLeft, ArrowRight, CircleCheck, FileSpreadsheet, FileText, LoaderCircle, Presentation } from 'lucide-react'
import type { ReviewStatus } from '@/lib/api'

export const statusNames: Record<ReviewStatus, string> = { queued: '排队中', running: '审查中', completed: '已完成', partial: '部分完成', failed: '审查失败', cancelled: '已取消' }

export function StatusBadge({ status }: { status: ReviewStatus }) {
  return <span className={`status-badge status-${status}`}>{status === 'running' || status === 'queued' ? <LoaderCircle size={14} aria-hidden="true" className={status === 'running' ? 'spin' : ''} /> : status === 'completed' ? <CircleCheck size={14} aria-hidden="true" /> : null}{statusNames[status] ?? status}</span>
}

export function FileIcon({ name, size = 22 }: { name: string, size?: number }) {
  const ext = name.split('.').pop()?.toLowerCase()
  const Icon = ext === 'xlsx' ? FileSpreadsheet : ext === 'pptx' ? Presentation : FileText
  return <span className={`file-icon file-${ext}`} aria-hidden="true"><Icon size={size} /></span>
}

export function ErrorState({ message, onRetry }: { message: string, onRetry?: () => void }) {
  return <div className="error-state" role="alert"><AlertCircle size={21} aria-hidden="true" /><div><strong>暂时无法完成操作</strong><p>{message}</p></div>{onRetry && <button className="button secondary small" onClick={onRetry}>重试</button>}</div>
}

export function Pagination({ page, total, size, onPage }: { page: number, total: number, size: number, onPage: (value: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / size))
  return <div className="pagination"><span>共 {total} 条 · 第 {page} / {pages} 页</span><div><button aria-label="上一页" className="icon-button" disabled={page <= 1} onClick={() => onPage(page - 1)}><ArrowLeft size={16} /></button><button aria-label="下一页" className="icon-button" disabled={page >= pages} onClick={() => onPage(page + 1)}><ArrowRight size={16} /></button></div></div>
}

export function formatDate(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}

export function formatSize(bytes: number) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function locationText(location: Record<string, unknown>) {
  const names: Record<string, string> = { page: '页', page_number: '页', slide: '幻灯片', slide_number: '幻灯片', sheet: '工作表', sheet_name: '工作表', cell: '单元格', paragraph: '段落', paragraph_index: '段落', table: '表格', shape_name: '形状', notes_paragraph: '备注段落', row: '行', column: '列' }
  return Object.entries(names).filter(([key]) => typeof location?.[key] === 'string' || typeof location?.[key] === 'number').map(([key, name]) => `${name} ${location[key]}`).slice(0, 5).join(' · ') || '原文位置'
}
