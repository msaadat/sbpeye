import { readonly, ref } from 'vue'
import { getLlmDebugStatus, type LlmDebugStatus } from '@/lib/api'

const state = ref<LlmDebugStatus>({ allowed: true, enabled: false, effective: false })
const loaded = ref(false)

async function refreshLlmDebugState() {
  try {
    state.value = await getLlmDebugStatus()
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
