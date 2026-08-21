import { computed, readonly, ref } from 'vue'

import { getLlmStatus, type LlmStatus } from '@/lib/api'

/**
 * The signed-in user's own LLM backend status, loaded once and shared.
 *
 * `/api/llm/status` probes the caller's provider, so it is answering about this account
 * and not the deployment. Two places care — the sidebar badge and the chat composer —
 * and the probe costs a round trip to the vendor, so it is fetched once and shared
 * rather than fetched per component.
 *
 * A failure resolves to `error` rather than rejecting: both callers render an indicator,
 * and "could not ask" is one of the things there is an indicator for.
 */
const status = ref<LlmStatus | null>(null)
const loading = ref(false)
const error = ref('')
let inFlight: Promise<void> | null = null

export function useLlmStatus() {
  async function load(force = false): Promise<void> {
    if (status.value && !force) return
    // Deduplicated: the sidebar and the chat view mount together on a page load of
    // /chat, and each would otherwise fire its own probe.
    if (inFlight && !force) return inFlight

    loading.value = true
    error.value = ''
    inFlight = (async () => {
      try {
        status.value = await getLlmStatus()
      } catch (err) {
        status.value = null
        error.value = err instanceof Error ? err.message : 'Unable to check LLM backend'
      } finally {
        loading.value = false
        inFlight = null
      }
    })()
    return inFlight
  }

  return {
    status: readonly(status),
    loading: readonly(loading),
    error: readonly(error),
    // This account has no provider credentials of its own, so chat will refuse the next
    // turn. Kept as a named flag because it is a prompt to go do something, not a fault.
    needsProviderKey: computed(() => status.value?.state === 'not_configured'),
    load,
  }
}
