/*
 * One reading of a circular's status for every place that colours it. The results
 * list, the detail header and the relationship graph each kept their own copy, and
 * they disagreed: "amended" was a blue Tag in the list, an unstyled grey chip in
 * the header and gold in the graph.
 *
 * Amended sits with superseded as a caution — the text in front of the reader is
 * not the whole of what is in force — which is the gold the graph already used.
 */
export type CircularStatusTone = 'success' | 'warn' | 'danger' | 'neutral'

export function circularStatusTone(status?: string | null): CircularStatusTone {
  const value = (status || '').toLowerCase()
  if (value.includes('active') || value.includes('indexed')) return 'success'
  if (value.includes('superseded') || value.includes('replaced') || value.includes('amended')) return 'warn'
  if (value.includes('withdrawn') || value.includes('cancel')) return 'danger'
  return 'neutral'
}

const TONE_COLOR: Record<CircularStatusTone, string> = {
  success: 'var(--sbp-success)',
  warn: 'var(--sbp-gold)',
  danger: 'var(--sbp-danger)',
  neutral: 'var(--sbp-muted)',
}

export function circularStatusColor(status?: string | null): string {
  return TONE_COLOR[circularStatusTone(status)]
}
