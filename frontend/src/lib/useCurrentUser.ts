import { computed, readonly, ref } from 'vue'

import { getCurrentUser, type CurrentUser } from '@/lib/api'

/**
 * The signed-in user, loaded once and shared.
 *
 * Module-level state rather than per-component: the sidebar, the settings page and the
 * admin console all need to know who is signed in and whether they are an admin, and
 * three independent fetches of the same thing would be three chances to disagree about
 * it mid-session.
 *
 * A failure is left as `null` rather than thrown. The only way to reach the app at all
 * is through the auth middleware, so "no user" here means the request 401'd — and the
 * API client has already started a redirect to the login page by the time this resolves.
 */
const user = ref<CurrentUser | null>(null)
const loading = ref(false)
let inFlight: Promise<void> | null = null

export function useCurrentUser() {
  async function load(force = false): Promise<void> {
    if (user.value && !force) return
    // Deduplicated: several components mount at once on first paint, and each would
    // otherwise fire its own /auth/me.
    if (inFlight && !force) return inFlight

    loading.value = true
    inFlight = (async () => {
      try {
        user.value = await getCurrentUser()
      } catch {
        user.value = null
      } finally {
        loading.value = false
        inFlight = null
      }
    })()
    return inFlight
  }

  function clear(): void {
    user.value = null
  }

  return {
    user: readonly(user),
    loading: readonly(loading),
    isAdmin: computed(() => Boolean(user.value?.is_admin)),
    email: computed(() => user.value?.email ?? ''),
    load,
    clear,
  }
}
