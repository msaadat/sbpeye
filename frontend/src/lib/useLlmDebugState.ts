import { readonly, ref } from 'vue'
import { getLlmDebugStatus, type LlmDebugStatus } from '@/lib/api'

const state = ref<LlmDebugStatus>({ allowed: true, enabled: false, effective: false })
const loaded = ref(false)

async function refreshLlmDebugState() {
  try {
    state.value = await getLlmDebugStatus()
  } catch {
    // The endpoint is admin-only and returns 403 for everyone else. Callers use this to
    // decide whether to show a debug panel, and "could not ask" and "not enabled" lead to
    // the same UI — so it resolves to the default rather than rejecting into a caller
    // that has nothing useful to do with the failure.
    state.value = { allowed: false, enabled: false, effective: false }
  } finally {
    loaded.value = true
  }
  return state.value
}

function setLlmDebugState(next: LlmDebugStatus) {
  state.value = next
  loaded.value = true
}

export function useLlmDebugState() {
  return {
    state: readonly(state),
    loaded: readonly(loaded),
    refreshLlmDebugState,
    setLlmDebugState,
  }
}
