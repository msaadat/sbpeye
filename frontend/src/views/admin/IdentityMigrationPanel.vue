<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Checkbox from 'primevue/checkbox'
import Message from 'primevue/message'
import { getIdentityMaintenance, getIdentityRecord, identityAction, type IdentityMaintenance, type IdentityMapping, type IdentityRecord } from '@/lib/api'

const emit = defineEmits<{ maintenance: [value: boolean] }>()
const state = ref<IdentityMaintenance | null>(null), error = ref(''), submitting = ref(false)
const selected = ref<IdentityMapping | null>(null), record = ref<IdentityRecord | null>(null)
const source = ref(''), note = ref(''), scopes = ref<string[]>([])
const attachmentUrls = ref<Record<string, string>>({}), confirmed = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined, disposed = false
const busy = computed(() => submitting.value || state.value?.busy)
const resume = computed(() => state.value?.apply_started && !state.value?.busy)
function accept(value: IdentityMaintenance) {
  if (state.value?.manifest_hash !== value.manifest_hash) confirmed.value = false
  state.value = value
  emit('maintenance', value.maintenance)
  if (selected.value) selected.value = value.mappings.find(row => row.old_id === selected.value?.old_id) || null
}
async function load() {
  clearTimeout(timer)
  try { const value = await getIdentityMaintenance(); if (!disposed) accept(value) }
  catch (exc) { error.value = exc instanceof Error ? exc.message : String(exc) }
  finally { if (!disposed) timer = setTimeout(() => void load(), state.value?.busy ? 1500 : 5000) }
}
async function act(action: string, body: unknown = {}) {
  submitting.value = true; error.value = ''
  try { accept(await identityAction(action, body)); await load() }
  catch (exc) { error.value = exc instanceof Error ? exc.message : String(exc) }
  finally { submitting.value = false }
}
async function inspect(row: IdentityMapping) {
  selected.value = row; record.value = null; source.value = ''; note.value = ''; scopes.value = []; attachmentUrls.value = {}
  try {
    const result = await getIdentityRecord(row.old_id)
    if (selected.value?.old_id === row.old_id) record.value = result
  } catch (exc) { error.value = exc instanceof Error ? exc.message : String(exc) }
}
async function readFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  if (file.size > 2_000_000) { error.value = 'Choose a source file smaller than 2 MB.'; return }
  source.value = await file.text()
}
function saveReview() {
  return act('review', { old_id: selected.value?.old_id, source_text: source.value, note: note.value, scopes: scopes.value,
    attachments: Object.entries(attachmentUrls.value).filter(([, url]) => url.trim()).map(([id, detection_url]) => ({ id, detection_url: detection_url.trim() })) })
}
onMounted(load)
onUnmounted(() => { disposed = true; clearTimeout(timer) })
</script>

<template>
  <Card class="identity-panel"><template #title>Circular identity migration</template><template #content>
    <Message v-if="error" severity="error" :closable="false">{{ error }}</Message>
    <p>Older slash-year references need a one-time ID correction before the first mirror audit. Prepare and review the changes here, then apply them with automatic backups.</p>
    <Message v-if="state?.maintenance" severity="warn" :closable="false">Maintenance is active. Normal searches, chat, downloads and sync are temporarily unavailable. This console and sign-in remain available. Keep maintenance active until migration finishes, or leave before applying.</Message>
    <Message v-if="state?.status === 'complete'" severity="success" :closable="false">Migration completed and affected indexes verified. Normal access is restored. You can now run the full mirror audit.</Message>
    <p v-if="state" role="status">Status: {{ state.status }}<span v-if="state.phase"> · {{ state.phase.replace(/_/g, ' ') }}</span><span v-if="state.busy"> · Working in the background; you can reload this page.</span></p>
    <Message v-if="state?.error" severity="error" :closable="false">{{ state.error }}</Message>
    <p v-if="state?.backup_directory">Backups and the reviewed manifest: {{ state.backup_directory }}</p>
    <div class="actions">
      <Button v-if="!state?.apply_started" :label="state?.maintenance ? 'Prepare review again' : 'Enter maintenance and prepare review'" :disabled="busy || !state" @click="act('prepare')" />
      <Button v-if="state?.can_cancel" label="Leave maintenance without applying" severity="secondary" :disabled="busy" @click="act('cancel')" />
      <Button v-if="resume" label="Resume migration" :disabled="busy" @click="act('apply', {manifest_hash: state?.manifest_hash})" />
      <Button label="Refresh status" text @click="load" />
    </div>
    <p v-if="state?.status === 'preparing'">Waiting for active requests and circular jobs to finish before capturing the review.</p>
    <template v-if="state?.maintenance && state.mappings.length">
      <p>{{ state.mappings.length }} circulars need ID changes. {{ state.conflicts.length }} have unresolved conflicts. {{ state.drift_count }} unrelated ID differences are excluded.</p>
      <div class="table-scroll"><table><thead><tr><th>Circular</th><th>Review</th><th></th></tr></thead><tbody>
        <tr v-for="row in state.mappings" :key="row.old_id"><td>{{ row.reference }}<details><summary>ID change</summary><p>{{ row.old_id }} → {{ row.new_id }}</p></details></td><td>{{ row.conflicts.length ? row.conflicts.map(item => item.replace(/_/g, ' ')).join('; ') : 'Ready' }}</td><td><Button label="Inspect" text :disabled="busy" @click="inspect(row)" /></td></tr>
      </tbody></table></div>
      <section v-if="selected && record" class="review">
        <h3>{{ selected.reference }}</h3>
        <a :href="selected.url" target="_blank" rel="noopener">Open the SBP source</a>
        <p>Compare the stored text, analyses and linked records with source evidence. Confirm only items you have verified belong to this circular and year. A matching database ID alone is insufficient.</p>
        <details><summary>Stored circular and analyses</summary><pre>{{ JSON.stringify(record.circular, null, 2) }}</pre></details>
        <details><summary>Linked records</summary><pre>{{ JSON.stringify(record.dependencies, null, 2) }}</pre><pre>{{ JSON.stringify(record.app_dependencies, null, 2) }}</pre></details>
        <details><summary>Attachments</summary><pre>{{ JSON.stringify(record.attachments, null, 2) }}</pre></details>
        <details v-if="Object.keys(record.review).length"><summary>Previously recorded evidence</summary><pre>{{ JSON.stringify(record.review, null, 2) }}</pre></details>
        <template v-if="state.status === 'review'">
          <label>Source evidence (HTML or text file)<input type="file" accept=".html,.htm,.txt,.json" @change="readFile"></label>
          <Button v-if="record.cached_html" label="Use cached source HTML" text @click="source = record.cached_html" />
          <label>Evidence text<textarea v-model="source" rows="7" maxlength="2000000" /></label>
          <div class="checks"><label><Checkbox v-model="scopes" value="analysis" /> I verified the stored analyses against this evidence</label><label><Checkbox v-model="scopes" value="dependents" /> I verified the linked records against this evidence</label></div>
          <p v-if="record.attachments.length">For each verified attachment, enter its original detected link. The server checks that the link occurs in the supplied HTML and produces the attachment's old ID. Leave unverified attachments blank.</p>
          <label v-for="attachment in record.attachments" :key="attachment.id">{{ attachment.filename }} — original detected URL<input v-model="attachmentUrls[attachment.id]" type="url" maxlength="2000" :placeholder="attachment.original_url"></label>
          <label>Review note: explain how the evidence establishes the circular and year<textarea v-model="note" rows="3" minlength="10" maxlength="5000" /></label>
          <Button label="Save evidence and recheck conflicts" :disabled="busy || !source.trim() || note.trim().length < 10 || (!scopes.length && !Object.values(attachmentUrls).some(url => url.trim()))" @click="saveReview" />
        </template>
      </section>
      <template v-if="state.can_apply">
        <p>Apply creates database backups on the data volume, changes the reviewed IDs and links, and rebuilds affected search entries. If interrupted, return here and resume.</p>
        <label class="confirmation"><Checkbox v-model="confirmed" binary /> I reviewed these mappings and want to apply the migration.</label>
        <Button label="Back up and apply migration" :disabled="busy || !confirmed" @click="act('apply', {manifest_hash: state.manifest_hash})" />
      </template>
    </template>
    <p v-else-if="state?.status === 'review'">No legacy slash-year IDs remain. Leave maintenance to return to the mirror audit.</p>
  </template></Card>
</template>

<style scoped>
.identity-panel { min-width: 0; overflow-wrap: anywhere; }
.actions, .checks { display: flex; flex-wrap: wrap; gap: .75rem; margin: 1rem 0; }
.checks label, .confirmation { display: flex; align-items: center; gap: .5rem; }
label { display: grid; gap: .4rem; margin: 1rem 0; }
textarea, input { width: 100%; min-width: 0; box-sizing: border-box; padding: .6rem; color: var(--p-text-color); background: var(--p-content-background); border: 1px solid var(--p-content-border-color); border-radius: .4rem; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 24rem; overflow: auto; font-size: .8rem; }
.review { margin: 1rem 0; padding: 1rem; border: 1px solid var(--p-content-border-color); border-radius: .5rem; }
.table-scroll { overflow-x: auto; }
table { width: 100%; min-width: 32rem; border-collapse: collapse; text-align: left; }
th, td { padding: .5rem; border-bottom: 1px solid var(--p-content-border-color); vertical-align: top; }
</style>
