import type { ReviewOptions } from '@/lib/api'

export function reviewOptions(points: string, notes: string): ReviewOptions {
  return { custom_points: points.split('\n').map(point => point.trim()).filter(Boolean), review_notes: notes.trim() }
}

export function reviewOptionsErrors(points: string, notes: string) {
  const options = reviewOptions(points, notes)
  return {
    points: options.custom_points.length > 10 ? '最多填写 10 项审查要点，每行一项。' : options.custom_points.some(point => point.length > 500) ? '每项审查要点不能超过 500 字。' : '',
    notes: notes.length > 2000 ? '审查备注不能超过 2000 字。' : '',
  }
}
