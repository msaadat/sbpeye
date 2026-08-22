/**
 * Formatting shared by the admin console tabs.
 *
 * One module because the same three questions come up on every tab — how big, how long
 * ago, what fraction — and five copies of "what do we print when the number is null"
 * is five chances to render `NaN%` at an operator who is trying to decide whether the
 * corpus is healthy.
 */

/** An em dash for absent values, so a missing number never reads as a zero. */
export const ABSENT = '—'

export function formatCount(value: number | null | undefined): string {
  return typeof value === 'number' ? value.toLocaleString() : ABSENT
}

export function formatDate(value?: string | null): string {
  if (!value) return ABSENT
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? ABSENT : parsed.toLocaleString()
}

export function formatBytes(value: number | null | undefined): string {
  if (typeof value !== 'number') return ABSENT
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let size = value / 1024
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size.toFixed(size >= 100 ? 0 : 1)} ${units[unit]}`
}

export function formatDuration(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number') return ABSENT
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  if (minutes < 60) return `${minutes}m ${rest}s`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

/**
 * `generated / total` as a percentage.
 *
 * Zero total returns 0 rather than NaN: an empty corpus has no coverage, and that is a
 * legible answer. The caller decides whether to show "0%" or "nothing to analyse yet".
 */
export function percent(generated: number, total: number): number {
  if (!total) return 0
  return Math.round((generated / total) * 100)
}

/** `{a: 1, b: 2}` as sorted `{label, count}` rows, largest first — for tables. */
export function facetRows(counts: Record<string, number> | undefined): Array<{ label: string; count: number }> {
  return Object.entries(counts || {})
    .map(([label, count]) => ({ label, count }))
    .sort((left, right) => right.count - left.count || left.label.localeCompare(right.label))
}

/** Turn `extraction_error` / `law_version` into `Extraction error` / `Law version`. */
export function humanize(value: string): string {
  const spaced = value.replace(/[._-]+/g, ' ').trim()
  return spaced ? spaced[0].toUpperCase() + spaced.slice(1) : value
}

/**
 * A tone name for a run, source, or reachability status.
 *
 * The vocabulary is shared across four tabs and three backends (`SyncStatus.status`,
 * `AIGenerationJob.status`, `SemanticIndexSource.status` and the verdicts from
 * `sbp_reachability`), which overlap but are not identical — hence one mapping rather
 * than a ternary per table cell.
 *
 * The reachability verdicts are here rather than local to the Sync tab because falling
 * through to `neutral` is not a harmless default for them: it renders `blocked` and
 * `reachable` as the same grey dot, on the one control that decides whether pressing
 * Start can do anything.
 */
export function statusTone(status: string): 'ok' | 'warn' | 'error' | 'busy' | 'neutral' {
  switch (status) {
    case 'success':
    case 'succeeded':
    case 'indexed':
    case 'reachable':
      return 'ok'
    case 'failed':
    case 'index_error':
    case 'extraction_error':
    case 'blocked':
    case 'no-outbound-http':
      return 'error'
    case 'stale':
    case 'empty':
    case 'unsupported':
    case 'completed_with_gaps':
    case 'intermittent':
    case 'intermittent-client-dependent':
      return 'warn'
    case 'running':
    case 'queued':
      return 'busy'
    default:
      return 'neutral'
  }
}
