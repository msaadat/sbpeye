/*
 * One reading of a circular's status for every place that shows it — the results list,
 * the detail header, the relationship graph — so a status looks and reads the same
 * everywhere (docs/REDESIGN_PLAN.md FN4). The three used to keep their own copies, and
 * "amended" came out blue in one, grey in another and gold in the third.
 *
 * Four states. Gold is reserved for "something changed", so amended is gold and
 * superseded is grey: the text in front of an amended reader still partly applies, a
 * superseded one is history.
 */
export type Standing = 'in-force' | 'amended' | 'superseded' | 'cancelled' | 'unknown'

export function circularStanding(status?: string | null): Standing {
  const value = (status || '').toLowerCase()
  if (value.includes('active') || value.includes('indexed')) return 'in-force'
  if (value.includes('amended')) return 'amended'
  if (value.includes('superseded') || value.includes('replaced')) return 'superseded'
  if (value.includes('withdrawn') || value.includes('cancel')) return 'cancelled'
  return 'unknown'
}

const LABELS: Record<Exclude<Standing, 'unknown'>, string> = {
  'in-force': 'In force',
  amended: 'Amended',
  superseded: 'Superseded',
  cancelled: 'Cancelled',
}

/** What a person reads: "In force", not the stored "active". */
export function standingLabel(status?: string | null): string {
  const standing = circularStanding(status)
  return standing === 'unknown' ? (status || '') : LABELS[standing]
}

/** The state's text colour, for places that colour a word rather than draw a chip. */
export function standingColor(status?: string | null): string {
  const standing = circularStanding(status)
  return standing === 'unknown' ? 'var(--sbp-muted)' : `var(--sbp-standing-${standing}-fg)`
}
