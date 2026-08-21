/**
 * Wording for controls a non-admin can see but not use.
 *
 * Actions that write the shared corpus — syncing, AI generation, re-fetching from SBP —
 * are admin-only on the server. The controls stay visible so the feature is discoverable
 * and the page does not change shape per account, but they are disabled and say why.
 * One place for the phrasing so eight buttons cannot drift into eight explanations.
 */
export function adminOnlyHint(action: string): string {
  return `${action} is limited to administrators on this deployment.`
}

/** For `aria-label`, where the full sentence would be read out on every focus. */
export function adminOnlyLabel(action: string): string {
  return `${action} — admin only`
}

/** For empty states, where the missing content is the point rather than the button. */
export const ADMIN_ONLY_EMPTY_HINT = 'Ask an administrator to generate it.'
