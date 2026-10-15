import { useEffect, useRef, type KeyboardEvent } from 'react'
import { Trash2 } from 'lucide-react'

export function ConfirmDelete({ busy, error, onCancel, onConfirm }: { busy: boolean, error?: string, onCancel: () => void, onConfirm: () => void }) {
  const dialog = useRef<HTMLElement>(null)

  useEffect(() => {
    const previous = document.activeElement
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    dialog.current?.querySelector('button')?.focus()
    return () => {
      document.body.style.overflow = overflow
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus()
    }
  }, [])

  function handleKey(event: KeyboardEvent<HTMLElement>) {
    if (event.key === 'Escape' && !busy) { event.preventDefault(); onCancel() }
    if (event.key !== 'Tab') return
    const controls = dialog.current?.querySelectorAll<HTMLButtonElement>('button:not([disabled])')
    if (!controls?.length) { event.preventDefault(); return }
    const first = controls[0]
    const last = controls[controls.length - 1]
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
  }

  return <div className="modal-backdrop"><section ref={dialog} className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-title" aria-describedby="delete-description" onKeyDown={handleKey}><Trash2 size={25} /><h2 id="delete-title">删除这条审查记录？</h2><p id="delete-description">上传文件、中间结果及审查报告将一并删除，此操作无法撤销。</p>{error && <p role="alert" className="validation-message">{error}</p>}<div><button className="button secondary" disabled={busy} onClick={onCancel}>保留记录</button><button className="button danger" disabled={busy} onClick={onConfirm}>{busy ? '正在删除…' : '确认删除'}</button></div></section></div>
}
