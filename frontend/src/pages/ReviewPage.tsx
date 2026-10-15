import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDownToLine, ArrowLeft, Check, ChevronDown, Clock3, FileCheck2, LoaderCircle, RotateCcw, Square, TriangleAlert } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'
import { ErrorState, FileIcon, formatDate, formatSize, locationText, Pagination, StatusBadge } from '@/components/Shared'
import { ReviewOptionsFields } from '@/components/ReviewOptionsFields'
import { reviewOptions, reviewOptionsErrors } from '@/lib/reviewOptions'
import { reviewStageName } from '@/lib/stages'
import { api, isActive, type Issue, type Review, type ReviewOptions } from '@/lib/api'

const categoryNames: Record<Issue['category'], string> = { typo: '文字错误', punctuation: '标点错误', numeric: '数值不一致', semantic: '语义不一致', custom: '专项审查' }
const severityNames: Record<Issue['severity'], string> = { high: '高', medium: '中', low: '低' }

function IssueCard({ issue, review, number, open, onToggle }: { issue: Issue, review: Review, number: number, open: boolean, onToggle: () => void }) {
  const missingFile = issue.finding_kind === 'missing' ? review.files.find(file => file.id === issue.missing_document_id) : undefined
  const missingCoverage = (issue.absence_coverage ?? []).filter(coverage => coverage.document_id === issue.missing_document_id)
  const absent = issue.finding_kind === 'missing' && Boolean(issue.missing_document_id)
  return <article className="issue-card"><button className="issue-header" aria-expanded={open} aria-controls={`issue-${issue.id}`} onClick={onToggle}>
    <span className="issue-number">{String(number).padStart(2, '0')}</span><span className="issue-heading"><span><span className={`issue-category category-${issue.category}`}>{categoryNames[issue.category]}</span><span className="scope-label">{issue.scope === 'cross' ? '跨文件' : '主文件'}</span>{issue.confidence === 'needs_confirmation' && <span className="confirmation-label">待确认</span>}</span><strong>{issue.title}</strong></span><span className={`severity severity-${issue.severity}`}>{severityNames[issue.severity]}优先级</span><ChevronDown size={18} aria-hidden="true" className={open ? 'expanded-chevron' : ''} />
  </button>{open && <div id={`issue-${issue.id}`} className="issue-body"><p>{issue.description}</p>{issue.category === 'custom' && issue.custom_rule_title && <p className="custom-rule-reference">审查要点：{issue.custom_rule_title}</p>}<div className={`evidence-grid ${issue.evidence.length + Number(absent) > 1 ? 'has-pair' : ''}`}>{absent && <div className="evidence absence-evidence"><div><FileIcon name={missingFile?.filename ?? 'document.docx'} size={17} /><strong>{missingFile?.filename ?? '待核对文件'}</strong>{missingFile?.is_main && <span className="main-badge">主文件</span>}</div><p>未找到所需内容</p><footer>{missingCoverage.length > 0 && missingCoverage.every(coverage => coverage.complete) ? '已核对该文件全部可提取内容。' : '审查覆盖不完整，需人工确认是否缺失。'}</footer></div>}{issue.evidence.map((evidence, index) => {
    const file = review.files.find(item => item.id === evidence.document_id)
    return <blockquote key={`${evidence.block_id}-${index}`} className="evidence"><div><FileIcon name={file?.filename ?? 'document.docx'} size={17} /><strong>{file?.filename ?? '来源文件'}</strong>{file?.is_main && <span className="main-badge">主文件</span>}{evidence.purpose === 'context' && <span className="scope-label">定位上下文</span>}</div><p>“{evidence.quote}”</p><footer>{locationText(evidence.location)}</footer></blockquote>
  })}</div><div className="suggestion"><strong>修改建议</strong><p>{issue.suggestion || '请结合原文核实，并统一相关表述。'}</p></div>{issue.annotation_status !== 'not_applicable' && <div className="annotation-note"><FileCheck2 size={16} aria-hidden="true" />{['added', 'annotated', 'success'].includes(issue.annotation_status) ? '已添加到 DOCX 主文件批注' : `未添加批注${issue.annotation_reason ? `：${issue.annotation_reason}` : ''}`}</div>}</div>}</article>
}

export function ReviewPage() {
  const { id = '' } = useParams()
  const [page, setPage] = useState(1)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [pointsDraft, setPointsDraft] = useState<string | null>(null)
  const [notesDraft, setNotesDraft] = useState<string | null>(null)
  const client = useQueryClient()
  const reviewQuery = useQuery({ queryKey: ['review', id], queryFn: () => api.review(id), refetchInterval: query => query.state.data && isActive(query.state.data.status) ? 2000 : false })
  const review = reviewQuery.data
  const issueQuery = useQuery({ queryKey: ['issues', id, page, review?.issue_count, review?.status], queryFn: () => api.issues(id, page), enabled: !!review, refetchInterval: review && isActive(review.status) ? 2000 : false })
  const issueTotal = issueQuery.data?.total
  useEffect(() => {
    if (issueTotal !== undefined) setPage(current => Math.min(current, Math.max(1, Math.ceil(issueTotal / 20))))
  }, [issueTotal])
  const action = useMutation({ mutationFn: (value: 'cancel' | 'retry' | { action: 'retry', review_options: ReviewOptions }) => typeof value === 'string' ? api.action(id, value) : api.action(id, value.action, value.review_options), onSuccess: async (result, value) => {
    if (value !== 'cancel') {
      setPage(1)
      setExpanded({})
    }
    setPointsDraft(null)
    setNotesDraft(null)
    client.setQueryData(['review', id], result)
    await client.invalidateQueries({ queryKey: ['review', id] })
    await client.invalidateQueries({ queryKey: ['issues', id] })
    await client.invalidateQueries({ queryKey: ['reviews'] })
  } })

  if (reviewQuery.isPending) return <main id="main-content" tabIndex={-1} className="page"><div className="loading-state" role="status">正在读取审查任务…</div></main>
  if (reviewQuery.isError || !review) return <main id="main-content" tabIndex={-1} className="page"><Link to="/history" className="back-link"><ArrowLeft size={16} aria-hidden="true" />返回审查记录</Link><ErrorState message={reviewQuery.error?.message ?? '任务不存在或已到期。'} onRetry={() => void reviewQuery.refetch()} /></main>

  const active = isActive(review.status)
  const cancelling = active && (review.cancel_requested || action.isPending && action.variables === 'cancel')
  const progress = review.progress_total > 0 ? Math.min(100, Math.max(0, Math.round(review.progress_current / review.progress_total * 100))) : 0
  const warnings = review.warnings ?? []
  const incomplete = review.status === 'partial' || warnings.some(warning => warning.incomplete !== false)
  const previous = (active || review.status === 'failed' || review.status === 'cancelled') && (review.artifacts.length > 0 || review.issue_count > 0)
  const successful = review.status === 'completed' && !incomplete
  const excel = review.artifacts.find(artifact => artifact.name.endsWith('.xlsx'))
  const docx = review.artifacts.find(artifact => artifact.name.endsWith('.docx'))
  const others = review.artifacts.filter(artifact => artifact.id !== excel?.id && artifact.id !== docx?.id)
  const requirements = review.review_requirements
  const rejected = requirements?.status === 'rejected'
  const draftPoints = pointsDraft ?? (review.review_options?.custom_points ?? []).join('\n')
  const draftNotes = notesDraft ?? review.review_options?.review_notes ?? ''
  const optionsErrors = reviewOptionsErrors(draftPoints, draftNotes)
  const normalized = requirements?.normalized
  const hasRequirements = Boolean(normalized?.custom_points.length || normalized?.notes.length || review.review_options?.custom_points.length || review.review_options?.review_notes)
  const optionsChanged = JSON.stringify(reviewOptions(draftPoints, draftNotes)) !== JSON.stringify(review.review_options ?? { custom_points: [], review_notes: '' })

  return <main id="main-content" tabIndex={-1} className="page review-page">
    <Link to="/history" className="back-link"><ArrowLeft size={16} aria-hidden="true" />审查记录</Link>
    <div className="page-heading review-heading"><div><h1>{review.title || '文件审查'}</h1></div><StatusBadge status={review.status} /></div>
    {active && <section className="panel progress-panel" aria-label="任务进度"><div className="progress-title" role="status" aria-live="polite"><LoaderCircle size={22} className="spin" aria-hidden="true" /><h2>{cancelling ? '正在取消' : reviewStageName(review.stage)}</h2></div><div className="progress-content"><p>{cancelling ? '正在停止审查，请稍候。' : '可离开本页，稍后从审查记录继续查看。'}</p>{review.progress_total > 0 && <span className="stage-count">本阶段已完成 {review.progress_current} / {review.progress_total}</span>}<div className="progress-track" role="progressbar" aria-label="本阶段进度" aria-valuemin={0} aria-valuemax={review.progress_total || undefined} aria-valuenow={review.progress_total > 0 ? review.progress_current : undefined}><span className={review.progress_total > 0 ? '' : 'indeterminate'} style={review.progress_total > 0 ? { width: `${progress}%` } : undefined} /></div></div><button disabled={cancelling || action.isPending} className="button secondary" onClick={() => action.mutate('cancel')}><Square size={14} aria-hidden="true" />{cancelling ? '正在取消' : '取消任务'}</button></section>}
    {!active && <section className="panel result-summary"><div><h2>{review.status === 'completed' ? '审查完成' : review.status === 'partial' ? '部分审查完成' : review.status === 'cancelled' ? '任务已取消' : '审查未完成'}</h2><p>{successful ? <>发现 <strong>{review.issue_count}</strong> 项问题</> : review.status === 'partial' ? <>已发现 <strong>{review.issue_count}</strong> 项问题，请留意未覆盖内容。</> : rejected ? '请调整补充审查要求后重新提交。' : '可以重试任务，继续完成审查。'}</p></div>{review.status !== 'completed' && !rejected && <button disabled={action.isPending} className="button secondary" onClick={() => action.mutate('retry')}><RotateCcw size={16} aria-hidden="true" />{action.isPending ? '正在处理…' : '重试任务'}</button>}</section>}
    {review.error && !rejected && <ErrorState message={review.error} />}{action.isError && <ErrorState message={action.error.message} />}
    {rejected && <section className="panel requirements-rejected" aria-labelledby="requirements-rejected-title"><h2 id="requirements-rejected-title">补充审查要求需要调整</h2>{requirements.reasons.length > 0 && <ul className="requirements-reasons">{requirements.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>}{!active ? <><p className="field-hint">已保留上传文件，修改要求后可直接重试。</p><ReviewOptionsFields idPrefix="retry-options" points={draftPoints} notes={draftNotes} disabled={action.isPending} onPointsChange={value => {
      setPointsDraft(value)
      action.reset()
    }} onNotesChange={value => {
      setNotesDraft(value)
      action.reset()
    }} /><button className="button primary" disabled={action.isPending || Boolean(optionsErrors.points || optionsErrors.notes)} onClick={() => action.mutate({ action: 'retry', review_options: reviewOptions(draftPoints, draftNotes) })}>{action.isPending ? '正在提交…' : '修改并重试'}<RotateCcw size={16} aria-hidden="true" /></button></> : <p className="field-hint">任务停止后可修改要求并重试。</p>}</section>}
    {(hasRequirements || !active) && !rejected && <details className="panel requirements-info"><summary><span>审查要求</span><ChevronDown size={18} className="disclosure-chevron" aria-hidden="true" /></summary><div className="requirements-content">{normalized ? <>{normalized.custom_points.length > 0 && <section><h3>专项审查要点</h3><ol className="normalized-rules">{normalized.custom_points.map(rule => <li key={rule.id}><strong>{rule.title}</strong><p>{rule.criterion}</p></li>)}</ol></section>}{normalized.notes.length > 0 && <section><h3>审查备注</h3><ul className="normalized-notes">{normalized.notes.map((note, index) => <li key={index}>{note}</li>)}</ul></section>}</> : hasRequirements && <p>{active ? '补充要求正在整理，确认后将显示实际采用的审查规则。' : '补充要求尚未完成确认，可修改后重试。'}</p>}{!active && <section className="requirements-edit"><h3>{hasRequirements ? '修改审查要求' : '补充审查要求'}</h3><p className="field-hint">已保留上传文件，修改后将按新要求重新审查。</p><ReviewOptionsFields idPrefix="edit-options" points={draftPoints} notes={draftNotes} disabled={action.isPending} onPointsChange={value => {
      setPointsDraft(value)
      action.reset()
    }} onNotesChange={value => {
      setNotesDraft(value)
      action.reset()
    }} /><button className="button secondary" disabled={action.isPending || !optionsChanged || Boolean(optionsErrors.points || optionsErrors.notes)} onClick={() => action.mutate({ action: 'retry', review_options: reviewOptions(draftPoints, draftNotes) })}>{action.isPending ? '正在提交…' : '修改并重试'}<RotateCcw size={16} aria-hidden="true" /></button></section>}</div></details>}
    {review.artifacts.length > 0 && <section className="report-actions" aria-label={previous ? '上次结果下载' : '下载报告'}>{previous && <span className="previous-label">上次结果</span>}{excel && <a href={api.download(id, 'artifacts', excel.id)} className="button primary" download><ArrowDownToLine size={17} aria-hidden="true" />下载 Excel 报告</a>}{docx && <a href={api.download(id, 'artifacts', docx.id)} className="button secondary" download><ArrowDownToLine size={17} aria-hidden="true" />下载批注文件</a>}{others.length > 0 && <details className="more-downloads"><summary>更多下载<ChevronDown size={16} aria-hidden="true" /></summary><div>{others.map(artifact => <a key={artifact.id} href={api.download(id, 'artifacts', artifact.id)} download>{artifact.name.endsWith('.json') ? 'JSON 审查结果' : artifact.name}</a>)}</div></details>}</section>}
    {warnings.length > 0 && <details className={`panel coverage-panel ${incomplete ? 'coverage-incomplete' : ''}`}><summary><TriangleAlert size={20} aria-hidden="true" /><span><strong>{incomplete ? '审查覆盖不完整' : '解析说明'}</strong><small>查看 {warnings.length} 项解析与覆盖说明</small></span><ChevronDown size={18} className="disclosure-chevron" aria-hidden="true" /></summary><div className="coverage-list">{warnings.map((warning, index) => <div className="coverage-item" key={`${warning.code}-${index}`}><strong>{warning.filename || review.files.find(file => file.id === warning.document_id)?.filename || '文件解析'}</strong><p>{warning.message}</p>{warning.location && <small>{locationText(warning.location)}</small>}</div>)}</div></details>}
    <section className="results-column"><div className="section-heading results-heading"><h2>{previous ? '上次审查问题' : '审查问题'}{(!active || previous) && <span className="count-badge">{review.issue_count}</span>}</h2></div>
      {issueQuery.isPending ? <div className="panel loading-state" role="status">正在读取问题…</div> : issueQuery.isError ? <ErrorState message={issueQuery.error.message} onRetry={() => void issueQuery.refetch()} /> : issueQuery.data.items.length === 0 && (issueQuery.data.total > 0 || review.issue_count > 0) ? <div className="panel empty-state"><span className="empty-result-icon"><TriangleAlert size={26} aria-hidden="true" /></span><h3>当前页暂无问题</h3><p>审查结果中仍有问题，请返回第一页或重新读取列表查看。</p><button className="button secondary" onClick={() => page > 1 ? setPage(1) : void issueQuery.refetch()}>{page > 1 ? '返回第一页' : '重新读取问题'}</button></div> : issueQuery.data.items.length === 0 ? <div className={`panel empty-state ${successful ? 'successful-empty' : ''}`}><span className="empty-result-icon">{successful ? <Check size={26} aria-hidden="true" /> : active ? <Clock3 size={26} aria-hidden="true" /> : <TriangleAlert size={26} aria-hidden="true" />}</span><h3>{active ? '正在核对文件内容' : successful ? '未发现审查问题' : '暂无审查问题可展示'}</h3><p>{active ? '问题将在证据复核完成后展示。' : successful ? '本次审查范围内未发现明确问题，仍可下载报告。' : '本次审查未完成或覆盖不完整，请结合任务状态使用结果。'}</p></div> : <><div className="issue-list">{issueQuery.data.items.map((issue, index) => {
        const open = expanded[issue.id] ?? (page === 1 && index === 0)
        return <IssueCard key={issue.id} issue={issue} review={review} number={(page - 1) * 20 + index + 1} open={open} onToggle={() => setExpanded(current => ({ ...current, [issue.id]: !open }))} />
      })}</div><Pagination page={page} total={issueQuery.data.total} size={20} onPage={setPage} /></>}
    </section>
    <details className="panel task-info"><summary><span>文件与任务信息</span><ChevronDown size={18} className="disclosure-chevron" aria-hidden="true" /></summary><div className="task-info-content"><dl><div><dt>创建时间</dt><dd>{formatDate(review.created_at)}</dd></div><div><dt>到期时间</dt><dd>{formatDate(review.expires_at)}</dd></div></dl><div className="source-list">{review.files.map(file => <a key={file.id} className="source-file" href={api.download(id, 'files', file.id)} download><FileIcon name={file.filename} size={21} /><div><strong>{file.filename}</strong><span>{file.is_main ? '主文件' : '对照文件'} · {formatSize(file.size)}</span></div><ArrowDownToLine size={17} aria-hidden="true" /></a>)}</div></div></details>
  </main>
}
