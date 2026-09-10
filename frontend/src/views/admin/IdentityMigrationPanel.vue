<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Checkbox from 'primevue/checkbox'
import Message from 'primevue/message'
import { getIdentityMaintenance, identityAction, type IdentityMaintenance } from '@/lib/api'

const emit = defineEmits<{ maintenance: [value: boolean] }>()
const state = ref<IdentityMaintenance | null>(null), error = ref(''), submitting = ref(false), confirmed = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined, disposed = false, requestVersion = 0
const busy = computed(() => submitting.value || state.value?.busy)
const resume = computed(() => state.value?.apply_started && !state.value?.busy)
const statusLabel = computed(() => ({
  idle: 'Ready', preparing: 'Finding affected circulars', review: 'Affected list ready',
  removing: 'Backing up and removing circulars', applying: 'Finishing identity migration',
  reviewing: 'Checking saved review', failed: 'Needs attention', interrupted: 'Interrupted — ready to resume',
  complete: 'Complete', cancelled: 'Maintenance ended',
})[state.value?.status || 'idle'] || state.value?.status)
function accept(value: IdentityMaintenance) {
  if (state.value?.removal_hash !== value.removal_hash || state.value?.status !== value.status) confirmed.value = false
  state.value = value
  emit('maintenance', value.maintenance)
}
async function load() {
  clearTimeout(timer)
  const version = ++requestVersion
  try {
    const value = await getIdentityMaintenance()
    if (!disposed && version === requestVersion) accept(value)
  } catch (exc) { error.value = exc instanceof Error ? exc.message : String(exc) }
  finally { if (!disposed && version === requestVersion) timer = setTimeout(() => void load(), state.value?.busy ? 1500 : 5000) }
}
async function act(action: string, body: unknown = {}) {
  ++requestVersion; clearTimeout(timer)
  submitting.value = true; error.value = ''
  try { accept(await identityAction(action, body)) }
  catch (exc) { error.value = exc instanceof Error ? exc.message : String(exc) }
  finally { submitting.value = false; await load() }
}
function resumeOperation() {
  return state.value?.operation === 'remove_legacy'
    ? act('remove', { removal_hash: state.value.removal_hash })
    : act('apply', { manifest_hash: state.value?.manifest_hash })
}
onMounted(load)
onUnmounted(() => { disposed = true; ++requestVersion; clearTimeout(timer) })
</script>

<template>
  <Card class="identity-panel"><template #title>Remove legacy circulars for mirror rebuild</template><template #content>
    <p>Clear the older circular IDs that block the mirror. This backs up and removes only the affected circulars so you can download fresh copies through the mirror.</p>
    <Message v-if="error" severity="error" :closable="false">{{ error }}</Message>
    <Message v-if="state?.maintenance" severity="warn" :closable="false">Maintenance is active. Other app requests are paused; this console and sign-in remain available.</Message>
    <Message v-if="state?.status === 'complete' && state.operation === 'remove_legacy'" severity="success" :closable="false">
      Removed {{ state.removed_count }} legacy circulars and their search entries. Normal access is restored. Run the full audit below, then start backfill to download the missing circulars.
    </Message>
    <Message v-else-if="state?.status === 'complete'" severity="success" :closable="false">Identity migration completed. You can now run the full mirror audit.</Message>
    <p v-if="state" role="status">{{ statusLabel }}<span v-if="state.busy">. Working in the background; you can reload this page.</span></p>
    <Message v-if="state?.error" severity="error" :closable="false">{{ state.error }}</Message>
    <div class="actions">
      <Button v-if="!state?.apply_started" :label="state?.maintenance ? 'Refresh affected list' : 'Find affected circulars'" :disabled="busy || !state" @click="act('prepare')" />
      <Button v-if="state?.can_cancel" label="Leave maintenance without removing" severity="secondary" :disabled="busy" @click="act('cancel')" />
      <Button v-if="resume" :label="state?.operation === 'remove_legacy' ? 'Resume removal' : 'Resume migration'" :disabled="busy" @click="resumeOperation" />
      <Button label="Refresh status" text @click="load" />
    </div>
    <p v-if="state?.status === 'preparing'">Waiting for active requests and circular jobs to finish before preparing the affected list.</p>
    <template v-if="state?.maintenance && !state.apply_started && state.status === 'review'">
      <p v-if="!state.removal_hash">Click <strong>Refresh affected list</strong> to prepare removal. No source-evidence review is needed.</p>
      <template v-else-if="state.removal_count">
        <p><strong>{{ state.removal_count }} circulars</strong> and {{ state.removal_attachment_count }} attachment records will be removed from the live corpus, together with their saved AI analyses and search entries. Archived files, research notes and chat history are kept.</p>
        <p>Saved circular links will resolve again after their replacements are downloaded. In backfill, enable <strong>Fetch attachments</strong> to restore attachments and select any AI analyses you want regenerated.</p>
        <details><summary>Show affected circulars ({{ state.removal_count }})</summary><ul><li v-for="row in state.mappings" :key="row.old_id">{{ row.reference }}</li></ul></details>
        <Message v-if="state.removal_conflicts?.length" severity="error" :closable="false">Removal is blocked by structural conflicts:<ul><li v-for="item in state.removal_conflicts" :key="item.old_id">{{ state.mappings.find(row => row.old_id === item.old_id)?.reference }}: {{ item.reasons.join('; ') }}</li></ul></Message>
        <template v-if="state.can_remove">
          <label class="confirmation"><Checkbox v-model="confirmed" binary /> I understand these {{ state.removal_count }} circulars will be backed up and removed, and I will restore them through mirror backfill.</label>
          <Button :label="`Back up and remove ${state.removal_count} circulars`" severity="danger" :disabled="busy || !confirmed" @click="act('remove', { removal_hash: state.removal_hash })" />
        </template>
      </template>
      <p v-else>No legacy circular IDs need removal. Leave maintenance and run the mirror audit.</p>
    </template>
    <p v-if="state?.backup_directory">Backup location: {{ state.backup_directory }}</p>
    <p v-if="resume">The operation has started. Resume it to finish cleanup and restore normal access.</p>
  </template></Card>
</template>

<style scoped>
.identity-panel { min-width: 0; overflow-wrap: anywhere; }
.actions { display: flex; flex-wrap: wrap; gap: .75rem; margin: 1rem 0; }
.confirmation { display: flex; align-items: flex-start; gap: .65rem; margin: 1.25rem 0; }
details { margin: 1rem 0; }
li { margin: .35rem 0; }
</style>
