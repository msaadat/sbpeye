<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import Message from 'primevue/message'
import { requestJson } from '@/lib/api'

type Action = 'attachments' | 'ai' | 'index' | 'body'
interface DocumentRow {
  id: string; reference: string; title: string; department: string; year: number | null;
  body: string; attachments: string; files: { id: string; filename: string; state: string; error?: string }[];
  ai: Record<string, string>; keyword: string; vectors: { id: string; label: string; state: string }[]; index_blocked: boolean;
}
interface Job {
  job_id: string; status: string; started_at: string; error?: string; selection: (string | { id: string })[];
  parameters: { action?: Action; operation: string; features?: string[] };
  progress: { items?: { id: string; feature?: string; status: string; error?: string }[]; cancel_requested?: boolean };
}
const route = useRoute()
const section = computed(() => route.path.split('/').pop() || 'overview')
const rows = ref<DocumentRow[]>([]), jobs = ref<Job[]>([]), selected = ref<string[]>([])
const features = ref<string[]>([]), sourceJob = ref(String(route.query.source_job_id || ''))
const search = ref(''), issue = ref(''), department = ref(''), year = ref('')
const action = ref<Action>('attachments'), size = ref(50), all = ref(false)
const loading = ref(false), error = ref(''), measured = ref(''), verified = ref(false), preview = ref(false)
const opened = ref<Job | null>(null)
const featureNames = ['summary', 'tags', 'checklist', 'relationships', 'entities', 'consolidation']
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false
const active = (j: Job) => ['queued', 'running'].includes(j.status)
const busy = computed(() => loading.value || jobs.value.some(active))
function eligible(r: DocumentRow, operation = action.value) {
  if (operation === 'body') return r.body === 'missing'
  if (operation === 'attachments') return ['unscanned', 'needs_files'].includes(r.attachments)
  if (operation === 'ai') return features.value.some(f => ['missing', 'failed'].includes(r.ai[f] || ''))
  return !r.index_blocked && (['missing', 'stale'].includes(r.keyword) || r.vectors.some(v => ['missing', 'stale'].includes(v.state)))
}
const filtered = computed(() => rows.value.filter(r =>
  (!search.value || `${r.reference} ${r.title}`.toLowerCase().includes(search.value.toLowerCase())) &&
  (!department.value || r.department === department.value) && (!year.value || String(r.year) === year.value) &&
  (!issue.value || (issue.value === 'actionable' ? eligible(r) : issue.value === 'body' ? r.body === 'missing' : r.attachments === issue.value))))
const departments = computed(() => [...new Set(rows.value.map(r => r.department).filter(Boolean))].sort())
const years = computed(() => [...new Set(rows.value.map(r => r.year).filter(Boolean))].sort().reverse())
const targets = computed(() => {
  const candidates = filtered.value.filter(r => eligible(r) && (!selected.value.length || selected.value.includes(r.id)))
  return all.value ? candidates : candidates.slice(0, size.value)
})
const page = ref(1)
const visible = computed(() => filtered.value.slice((page.value - 1) * 50, page.value * 50))
const workbench = computed(() => ['documents', 'analysis', 'search-index'].includes(section.value))
const featureCounts = computed(() => featureNames.map(feature => ({ feature, count: rows.value.filter(r => ['missing', 'failed'].includes(r.ai[feature] || '')).length })))
const title = computed(() => ({ overview: 'Coverage overview', documents: 'Documents & attachments', analysis: 'AI analysis', 'search-index': 'Search readiness', jobs: 'Maintenance jobs' }[section.value] || 'Corpus maintenance'))
async function load(verify = false) {
  loading.value = true; error.value = ''
  try {
    const params = new URLSearchParams({ verify: String(verify) })
    if (sourceJob.value) params.set('source_job_id', sourceJob.value)
    const [assessment, history] = await Promise.all([
      requestJson<{ items: DocumentRow[]; generated_at: string; verified: boolean }>(`/admin/maintenance/documents?${params}`),
      requestJson<Job[]>('/admin/maintenance/jobs'),
    ])
    if (disposed) return
    rows.value = assessment.items; measured.value = assessment.generated_at; verified.value = assessment.verified; jobs.value = history
    if (opened.value) opened.value = history.find(j => j.job_id === opened.value?.job_id) || opened.value
  } catch (e) { error.value = String(e) }
  finally { loading.value = false; schedule() }
}
function schedule() {
  clearTimeout(timer)
  if (!disposed && jobs.value.some(active)) timer = setTimeout(() => void load(false), 3000)
}
async function post(path: string, body: unknown) {
  return requestJson<{ job_id: string }>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
}
async function start(ids = targets.value.map(r => r.id), operation = action.value, chosenFeatures = features.value) {
  loading.value = true; error.value = ''
  try {
    const result = await post('/circulars/maintenance/jobs', { ids, action: operation, features: chosenFeatures, size: size.value, source_job_id: sourceJob.value || null })
    opened.value = await requestJson<Job>(`/admin/maintenance/jobs/${result.job_id}`)
    preview.value = false; selected.value = []; await load()
  } catch (e) { error.value = String(e); loading.value = false }
}
async function cancel(j: Job) {
  try { await post(`/circulars/maintenance/jobs/${j.job_id}/cancel`, {}); await load() }
  catch (e) { error.value = String(e) }
}
function retry(j: Job) {
  const ids = j.progress.items?.filter(i => i.status !== 'completed').map(i => i.id) || []
  if (ids.length && j.parameters.action) void start(ids, j.parameters.action, j.parameters.features || [])
}
function follow(j: Job) {
  sourceJob.value = j.job_id; action.value = 'attachments'; issue.value = 'actionable'; void load()
}
watch([search, issue, department, year, features, selected, size, all], () => { preview.value = false; page.value = 1 }, { deep: true })
watch(section, () => {
  action.value = section.value === 'analysis' ? 'ai' : section.value === 'search-index' ? 'index' : 'attachments'
  issue.value = ''; selected.value = []; preview.value = false
}, { immediate: true })
onMounted(() => void load())
onUnmounted(() => { disposed = true; clearTimeout(timer) })
</script>

<template>
  <div class="maintenance-view">
    <header><h2>{{ title }}</h2><p>Audit circulars already held, inspect gaps, and process an exact selection.</p></header>
    <Message v-if="error" severity="error" :closable="false">{{ error }}</Message>
    <div class="toolbar"><Button label="Refresh status" :loading="loading" @click="load(false)" /><Button label="Verify search indexes" severity="secondary" :disabled="busy" @click="load(true)" /><span v-if="measured">{{ verified ? 'Index contents checked' : 'Recorded index status' }} · {{ new Date(measured).toLocaleString() }}</span></div>
    <div v-if="section === 'overview'" class="summary">
      <RouterLink to="/admin/mirror">SBP → database<br>Open listing audit</RouterLink>
      <RouterLink to="/admin/documents"><strong>{{ rows.filter(r => eligible(r, 'attachments')).length }}</strong> circulars need attachment work</RouterLink>
      <RouterLink to="/admin/analysis"><strong>{{ rows.filter(r => Object.values(r.ai).some(s => ['missing','failed'].includes(s))).length }}</strong> circulars lack AI analysis</RouterLink>
      <RouterLink to="/admin/search-index"><strong>{{ rows.filter(r => eligible(r, 'index')).length }}</strong> circulars need index repair</RouterLink>
    </div>
    <Message v-if="rows.some(r => r.index_blocked)" severity="warn" :closable="false">Embedding configuration differs from the stored index. Targeted vector repairs are blocked. Review configuration and the full rebuild procedure in System.</Message>
    <p v-if="section === 'overview'">Coverage is measured separately at each stage. Open a count to inspect documents and prepare a batch. Laws and regulation statistics remain available under Corpus statistics & laws.</p>
    <p v-if="section === 'documents'">Attachment discovery must run before “no attachments” is known. Unsupported files and unreadable scans remain visible as limitations.</p>
    <p v-if="section === 'analysis'">Completion records generation, not legal accuracy or freshness after source changes. Consolidation is listed once per existing chain, on its base circular.</p>
    <div v-if="section === 'analysis'" class="summary"><button v-for="f in featureCounts" :key="f.feature" @click="features = [f.feature]; issue = 'actionable'"><strong>{{ f.count }}</strong> missing {{ f.feature }}</button></div>
    <template v-if="workbench">
    <div class="filters">
      <label>Source batch<select v-model="sourceJob" @change="selected = []; load(false)"><option value="">All stored circulars</option><option v-for="j in jobs" :key="j.job_id" :value="j.job_id">{{ j.started_at }} · {{ j.parameters.action || 'backfill' }} · {{ j.job_id.slice(0,8) }}</option></select></label>
      <label>Reference or title<input v-model="search"></label>
      <label>Department<select v-model="department"><option value="">All</option><option v-for="d in departments" :key="d">{{ d }}</option></select></label>
      <label>Year<select v-model="year"><option value="">All</option><option v-for="y in years" :key="String(y)">{{ y }}</option></select></label>
      <label>Finding<select v-model="issue"><option value="">All</option><option value="actionable">Eligible for selected action</option><option value="unscanned">Attachments never scanned</option><option value="needs_files">Download/extraction problems</option><option value="limited">Unsupported/unreadable files</option><option value="body">Missing body text</option></select></label>
    </div>
    <section class="panel">
      <h3>Prepare a batch</h3>
      <div class="filters">
        <label>Action<select v-model="action" @change="preview = false"><option value="attachments">Fetch and index missing attachments</option><option value="body">Recover missing circular bodies</option><option value="ai">Generate missing AI analyses</option><option value="index">Repair missing/stale search entries</option></select></label>
        <label>Batch size<input v-model.number="size" type="number" min="1" max="500"></label>
        <label class="inline"><Checkbox v-model="all" binary />Process all eligible matches in batches</label>
      </div>
      <div v-if="action === 'ai'" class="toolbar"><label v-for="f in featureNames" :key="f" class="inline"><Checkbox v-model="features" :value="f" />{{ f }}</label></div>
      <p>{{ filtered.length }} matches · {{ selected.length }} explicitly selected · {{ targets.length }} eligible documents in this batch. Completed work is skipped. A batch continues when you close this page.</p>
      <Button label="Preview batch" :disabled="busy || !targets.length" @click="preview = true" />
      <div v-if="preview"><p>{{ targets.length }} documents will receive {{ action }} maintenance<span v-if="action === 'ai'">: {{ targets.reduce((n, r) => n + features.filter(f => ['missing', 'failed'].includes(r.ai[f] || '')).length, 0) }} feature tasks for {{ features.join(', ') }}. Relationships run before consolidation across the batch</span>.</p><details><summary>Exact selection</summary><ul><li v-for="r in targets" :key="r.id">{{ r.reference }} — {{ r.title }}</li></ul></details><Button label="Start batch" :disabled="busy" @click="start()" /></div>
    </section>
    <div class="table-wrap"><table><thead><tr><th>Select</th><th>Circular</th><th>Body / attachments</th><th>AI analysis</th><th>Search</th></tr></thead><tbody>
      <tr v-for="r in visible" :key="r.id"><td><Checkbox v-model="selected" :value="r.id" :aria-label="`Select ${r.reference}`" /></td><td><RouterLink :to="`/circulars/${r.id}`">{{ r.reference }}</RouterLink><p>{{ r.title }}</p><small>{{ r.department }} · {{ r.year }}</small></td><td>{{ r.body }} / {{ r.attachments }}<details v-if="r.files.length"><summary>{{ r.files.length }} files</summary><p v-for="f in r.files" :key="f.id">{{ f.filename }}: {{ f.state }} {{ f.error }}</p></details></td><td><div v-for="(state, feature) in r.ai" :key="feature">{{ feature }}: {{ state }}</div></td><td>Keyword: {{ r.keyword }}<details><summary>Vector sources</summary><p v-for="v in r.vectors" :key="v.id">{{ v.label }}: {{ v.state }}</p></details></td></tr>
    </tbody></table><p v-if="!filtered.length">No documents match these filters.</p></div>
    <div class="toolbar"><Button label="Previous" :disabled="page <= 1" @click="page--" /><span>Page {{ page }} · {{ filtered.length }} documents</span><Button label="Next" :disabled="page * 50 >= filtered.length" @click="page++" /><Button label="Select this page" severity="secondary" @click="selected = [...new Set([...selected, ...visible.map(r => r.id)])]" /><Button label="Clear selection" text @click="selected = []" /></div>
    </template>
    <section v-if="section === 'jobs' || opened || section === 'overview'" class="panel"><h3>Jobs and follow-up</h3><p>Retry creates a new job for unfinished or failed documents. Attachment follow-up uses the saved batch IDs across departments and years.</p>
      <div v-for="j in jobs" :key="j.job_id" class="job"><button @click="opened = j">{{ new Date(j.started_at).toLocaleString() }} · {{ j.parameters.action || 'Mirror backfill' }} · {{ j.status }}</button><RouterLink :to="{ path: '/admin/documents', query: { source_job_id: j.job_id } }" @click="follow(j)">Attachments for this batch</RouterLink><Button v-if="j.parameters.operation === 'maintenance' && !active(j)" label="Retry unfinished" text :disabled="busy" @click="retry(j)" /><Button v-if="j.parameters.operation === 'maintenance' && active(j)" :label="j.progress.cancel_requested ? 'Cancellation requested' : 'Cancel after current item'" text :disabled="!!j.progress.cancel_requested" @click="cancel(j)" /></div>
      <div v-if="opened"><h4>{{ opened.job_id }} · {{ opened.status }}</h4><p>{{ opened.error }}</p><ul><li v-for="i in opened.progress.items" :key="`${i.id}:${i.feature || ''}`">{{ rows.find(r => r.id === i.id)?.reference || i.id }} {{ i.feature }}: {{ i.status }} {{ i.error }}</li></ul></div>
    </section>
  </div>
</template>

<style scoped>
.maintenance-view { display:grid; gap:1rem } h2,h3,p { margin:.3rem 0 .7rem } .toolbar,.inline { display:flex; flex-wrap:wrap; gap:.7rem; align-items:center } .filters,.summary { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:1rem } label { display:grid; gap:.4rem } input,select { padding:.6rem; width:100%; min-width:0; background:var(--p-content-background); color:var(--p-text-color); border:1px solid var(--p-content-border-color); border-radius:6px } .panel,.summary > * { padding:1rem; border:1px solid var(--p-content-border-color); border-radius:10px; background:var(--p-content-background); color:var(--p-text-color) } .summary > * { text-align:left } .summary strong { display:block; font-size:1.5rem } .table-wrap { overflow:auto } table { border-collapse:collapse; width:100%; font-size:.875rem } th,td { text-align:left; padding:.7rem; border-bottom:1px solid var(--p-content-border-color); vertical-align:top } td:nth-child(2) { min-width:230px } .job { display:flex; flex-wrap:wrap; gap:.5rem; align-items:center; border-bottom:1px solid var(--p-content-border-color); padding:.5rem 0 } .job > button:first-child { background:none; border:0; color:var(--p-primary-color); cursor:pointer } details p { overflow-wrap:anywhere }
</style>
