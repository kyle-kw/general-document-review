import { reviewOptionsErrors } from '@/lib/reviewOptions'

export function ReviewOptionsFields({ points, notes, onPointsChange, onNotesChange, disabled = false, idPrefix = 'review-options' }: { points: string, notes: string, onPointsChange: (value: string) => void, onNotesChange: (value: string) => void, disabled?: boolean, idPrefix?: string }) {
  const errors = reviewOptionsErrors(points, notes)
  return <fieldset className="review-options-fields" disabled={disabled}>
    <div className="form-field"><label htmlFor={`${idPrefix}-points`}>审查要点</label><p id={`${idPrefix}-points-hint`} className="field-hint">每行一项，最多 10 项，每项不超过 500 字。</p><textarea id={`${idPrefix}-points`} rows={4} value={points} onChange={event => onPointsChange(event.target.value)} aria-invalid={Boolean(errors.points)} aria-describedby={`${idPrefix}-points-hint${errors.points ? ` ${idPrefix}-points-error` : ''}`} placeholder="例如：核对项目预算是否包含税费" />{errors.points && <p id={`${idPrefix}-points-error`} className="validation-message" role="alert">{errors.points}</p>}</div>
    <div className="form-field"><div className="field-label"><label htmlFor={`${idPrefix}-notes`}>审查备注</label><span>{notes.length} / 2000 字</span></div><p id={`${idPrefix}-notes-hint`} className="field-hint">可补充术语含义、核对口径或需要留意的背景。</p><textarea id={`${idPrefix}-notes`} rows={3} value={notes} onChange={event => onNotesChange(event.target.value)} aria-invalid={Boolean(errors.notes)} aria-describedby={`${idPrefix}-notes-hint${errors.notes ? ` ${idPrefix}-notes-error` : ''}`} />{errors.notes && <p id={`${idPrefix}-notes-error`} className="validation-message" role="alert">{errors.notes}</p>}</div>
  </fieldset>
}
