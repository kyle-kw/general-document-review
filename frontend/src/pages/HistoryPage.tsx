import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, ChevronDown, FolderOpen, Plus, Trash2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { ErrorState, FileIcon, formatDate, Pagination, StatusBadge } from '@/components/Shared'
import { ConfirmDelete } from '@/components/ConfirmDelete'
import { api, isActive } from '@/lib/api'

export function HistoryPage() {
  const [page, setPage] = useState(1)
  const [deleting, setDeleting] = useState<string | null>(null)
  const client = useQueryClient()
  const config = useQuery({ queryKey: ['config'], queryFn: api.config, staleTime: 60_000 })
  const reviews = useQuery({ queryKey: ['reviews', page], queryFn: () => api.reviews(page), refetchInterval: query => query.state.data?.items.some(review => isActive(review.status)) ? 2000 : false })
  const remove = useMutation({ mutationFn: api.remove, onSuccess: async () => { setDeleting(null); await client.invalidateQueries({ queryKey: ['reviews'] }); if (reviews.data?.items.length === 1 && page > 1) setPage(page - 1) } })

  return <main id="main-content" tabIndex={-1} className="page history-page"><div className="page-heading"><div><h1>审查记录</h1></div><Link to="/" className="button primary"><Plus size={17} aria-hidden="true" />新建审查</Link></div>
    {reviews.isPending ? <div role="status" className="loading-state">正在读取审查记录…</div> : reviews.isError ? <ErrorState message={reviews.error.message} onRetry={() => void reviews.refetch()} /> : <section className="panel history-panel" aria-label="审查任务"><div className="section-heading table-heading"><span>共 {reviews.data.total} 个任务</span></div>{reviews.data.items.length === 0 ? <div className="empty-state"><FolderOpen size={36} strokeWidth={1.5} aria-hidden="true" /><h2>还没有审查记录</h2><p>上传文件后，可在这里查看进度与报告。</p><Link className="button secondary" to="/">新建审查<ArrowRight size={16} aria-hidden="true" /></Link></div> : <><table className="history-table"><thead><tr><th>审查任务</th><th>状态</th><th>问题</th><th>创建 / 到期时间</th><th><span className="sr-only">操作</span></th></tr></thead><tbody>{reviews.data.items.map(review => <tr key={review.id}><td className="history-title"><Link to={`/reviews/${review.id}`} className="history-name"><FileIcon name={review.title || review.files.find(file => file.is_main)?.filename || 'document.docx'} /><div><strong>{review.title || '文件审查'}</strong><span>{review.files.length} 份文件</span></div></Link></td><td data-label="状态"><StatusBadge status={review.status} /></td><td data-label="问题"><span className="issue-total">{review.status === 'completed' || review.status === 'partial' ? `${review.issue_count} 项` : '—'}</span></td><td className="date-cell"><span><span className="sr-only">创建 </span>{formatDate(review.created_at)}</span><small>到期 {formatDate(review.expires_at)}</small></td><td className="history-actions"><div className="row-actions"><Link to={`/reviews/${review.id}`} className="text-link">查看<ArrowRight size={16} aria-hidden="true" /></Link>{!isActive(review.status) && <button className="icon-button danger-icon" aria-label={`删除 ${review.title}`} onClick={() => { remove.reset(); setDeleting(review.id) }}><Trash2 size={18} aria-hidden="true" /></button>}</div></td></tr>)}</tbody></table><Pagination page={page} total={reviews.data.total} size={10} onPage={setPage} /></>}</section>}
    <details className="about-records"><summary>关于记录<ChevronDown size={16} className="disclosure-chevron" aria-hidden="true" /></summary><p>任务结束后保留 {config.data?.retention_days ?? 30} 天。记录仅对当前浏览器可见，清除浏览器凭证后无法恢复访问。</p></details>
    {deleting && <ConfirmDelete busy={remove.isPending} error={remove.error?.message} onCancel={() => setDeleting(null)} onConfirm={() => remove.mutate(deleting)} />}
  </main>
}
