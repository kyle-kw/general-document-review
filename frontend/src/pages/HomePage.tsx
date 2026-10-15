import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, ChevronDown, Plus, UploadCloud, X } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { ErrorState, FileIcon, formatSize } from '@/components/Shared'
import { api, newSubmissionKey } from '@/lib/api'
import { ReviewOptionsFields } from '@/components/ReviewOptionsFields'
import { reviewOptions, reviewOptionsErrors } from '@/lib/reviewOptions'

const extensions = ['docx', 'xlsx', 'pptx', 'pdf']

export function HomePage() {
  const [files, setFiles] = useState<File[]>([])
  const [mainIndex, setMainIndex] = useState(0)
  const [validation, setValidation] = useState('')
  const [dragging, setDragging] = useState(false)
  const [points, setPoints] = useState('')
  const [notes, setNotes] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const keyRef = useRef(newSubmissionKey())
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const config = useQuery({ queryKey: ['config'], queryFn: api.config, staleTime: 60_000 })
  const create = useMutation({
    mutationFn: () => api.create(files, mainIndex, keyRef.current, reviewOptions(points, notes)),
    onSuccess: async (review) => {
      await queryClient.invalidateQueries({ queryKey: ['reviews'] })
      navigate(`/reviews/${review.id}`)
    },
  })
  const optionsErrors = reviewOptionsErrors(points, notes)

  function changed() {
    setValidation('')
    keyRef.current = newSubmissionKey()
    create.reset()
  }

  function addFiles(incoming: File[]) {
    if (create.isPending || incoming.length === 0) return
    const invalid = incoming.find(file => !extensions.includes(file.name.split('.').pop()?.toLowerCase() ?? ''))
    if (invalid) {
      setValidation(`“${invalid.name}”格式不支持，请选择 DOCX、XLSX、PPTX 或 PDF 文件。`)
      return
    }
    const next = [...files]
    incoming.forEach(file => { if (!next.some(existing => existing.name === file.name && existing.size === file.size && existing.lastModified === file.lastModified)) next.push(file) })
    if (config.data && next.length > config.data.max_files) {
      setValidation(`每个任务最多选择 ${config.data.max_files} 份文件。`)
      return
    }
    if (config.data && next.some(file => file.size > config.data!.max_file_mb * 1024 * 1024)) {
      setValidation(`单个文件不能超过 ${config.data.max_file_mb} MiB。`)
      return
    }
    setFiles(next)
    changed()
  }

  function removeFile(index: number) {
    setFiles(files.filter((_, item) => item !== index))
    setMainIndex(index === mainIndex ? 0 : index < mainIndex ? mainIndex - 1 : mainIndex)
    changed()
  }

  return <main id="main-content" tabIndex={-1} className="page home-page">
    <div className="page-heading"><div><h1>新建审查</h1><p className="page-intro">上传文件，检查文字错误与内容一致性。</p></div></div>
    {config.isError && <ErrorState message="无法读取审查配置，请重试。" onRetry={() => void config.refetch()} />}
    {config.data?.llm_configured === false && <div className="notice warning-notice">审查模型服务尚未配置，请联系管理员完成设置后运行任务。</div>}
    <section className="panel upload-panel" aria-label="审查文件">
      <div className={`dropzone ${files.length ? 'compact' : ''} ${dragging ? 'dragging' : ''}`} onDragOver={event => {
        event.preventDefault()
        setDragging(true)
      }} onDragLeave={() => setDragging(false)} onDrop={event => {
        event.preventDefault()
        setDragging(false)
        addFiles(Array.from(event.dataTransfer.files))
      }}>
        <UploadCloud size={files.length ? 24 : 36} strokeWidth={1.5} aria-hidden="true" />
        <div><h2>{files.length ? '继续添加文件' : '拖放文件到这里'}</h2>{!files.length && <p>或从电脑中选择文件</p>}</div>
        <button className="button secondary" disabled={create.isPending} onClick={() => inputRef.current?.click()}><Plus size={17} aria-hidden="true" />{files.length ? '添加文件' : '选择文件'}</button>
        <input ref={inputRef} aria-label="上传审查文件" aria-describedby={validation ? 'file-limits file-validation' : 'file-limits'} className="sr-only" tabIndex={-1} type="file" accept=".docx,.xlsx,.pptx,.pdf" multiple disabled={create.isPending} onChange={event => {
          addFiles(Array.from(event.target.files ?? []))
          event.target.value = ''
        }} />
      </div>
      <p id="file-limits" className="upload-limits">DOCX、XLSX、PPTX、PDF{config.data && <> · 最多 {config.data.max_files} 份 · 单文件 {config.data.max_file_mb} MiB 以内</>}</p>
      {validation && <p id="file-validation" className="validation-message" role="alert">{validation}</p>}
      {files.length > 0 && <div className="selected-files">
        <div className="section-heading"><h2>已选择 {files.length} 份文件</h2>{files.length > 1 && <span>请选择一个主文件</span>}</div>
        <fieldset disabled={create.isPending}><legend className="sr-only">选择主文件</legend>{files.map((file, index) => <div className={`selected-file ${index === mainIndex ? 'main-file' : ''}`} key={`${file.name}-${file.lastModified}-${file.size}`}>
          <FileIcon name={file.name} /><div className="file-name"><strong>{file.name}</strong><span>{formatSize(file.size)}</span></div>
          {files.length === 1 ? <span className="main-badge">主文件</span> : <label className="main-choice"><input type="radio" name="main-file" checked={index === mainIndex} onChange={() => {
            setMainIndex(index)
            changed()
          }} />{index === mainIndex ? '主文件' : '设为主文件'}</label>}
          <button type="button" aria-label={`移除 ${file.name}`} className="icon-button" onClick={() => removeFile(index)}><X size={18} aria-hidden="true" /></button>
        </div>)}</fieldset>
      </div>}
      <p className="review-rule">主文件检查错别字、标点及文内一致性；其他文件仅与主文件核对一致性。</p>
      <details className="review-options"><summary>补充审查要求<span className="optional-label">选填</span><ChevronDown size={18} className="disclosure-chevron" aria-hidden="true" /></summary><ReviewOptionsFields points={points} notes={notes} disabled={create.isPending} onPointsChange={value => {
        setPoints(value)
        changed()
      }} onNotesChange={value => {
        setNotes(value)
        changed()
      }} /></details>
      {create.isError && <ErrorState message={create.error.message} />}
      <div className="submit-row"><p className="retention-note">任务结束后保留 {config.data?.retention_days ?? 30} 天，可随时下载或删除。</p><button className="button primary" disabled={!files.length || !config.data?.llm_configured || config.isError || create.isPending || Boolean(optionsErrors.points || optionsErrors.notes)} onClick={() => create.mutate()}>{create.isPending ? '正在上传文件…' : '开始审查'}<ArrowRight size={18} aria-hidden="true" /></button></div>
    </section>
  </main>
}
