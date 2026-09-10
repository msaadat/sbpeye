<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Checkbox from 'primevue/checkbox'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import AdminStatusChip from '@/components/AdminStatusChip.vue'
import IdentityMigrationPanel from './IdentityMigrationPanel.vue'
import { getMirrorOverview, getMirrorGaps, getMirrorFindings, getMirrorJob, getMirrorAttempts, mirrorAction,
  type MirrorOverview, type MirrorGap, type MirrorFinding, type MirrorJob, type MirrorAttempt } from '@/lib/api'
import { formatDate, formatCount } from './adminFormat'

const overview = ref<MirrorOverview | null>(null)
const maintenanceActive = ref(false)
function maintenanceChanged(value: boolean) {
  const previous = maintenanceActive.value
  maintenanceActive.value = value
  if (value) { clearTimeout(timer); error.value = '' }
  else if (previous) void load()
}
const gaps = ref<MirrorGap[]>([]), findings = ref<MirrorFinding[]>([]), attempts = ref<MirrorAttempt[]>([])
const job = ref<MirrorJob | null>(null), error = ref(''), notice = ref(''), loading = ref(false), submitting = ref(false)
const page = ref(1), total = ref(0), findingPage = ref(1), findingTotal = ref(0), attemptPage = ref(1), attemptTotal = ref(0)
const status = ref('pending'), bucket = ref('missing'), q = ref(''), year = ref<number | null>(null), department = ref('')
const size = ref(50), workers = ref(2), delay = ref(0.5), maxAttempts = ref(3), attachments = ref(false), repeat = ref(false), includeFailed = ref(false)
const order = ref('newest_first'), selectedFeatures = ref<string[]>([]), skipReason = ref('')
const features = ['summary', 'tags', 'checklist', 'relationships', 'entities', 'consolidation']
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false
let pendingReload = false
const storedJob = ref(localStorage.getItem('sbpeye-mirror-job') || '')
const active = (value?: string) => value === 'running' || value === 'queued'
const busy = computed(() => submitting.value || active(overview.value?.audit?.status) || active(overview.value?.active_job?.status) || active(job.value?.status))
const baseline = computed(() => overview.value?.latest_complete_audit)
const query = computed(() => {
  const params = new URLSearchParams({ page: String(page.value), per_page: '50' })
  if (status.value) params.set('status', status.value)
  if (q.value) params.set('q', q.value)
  if (year.value) params.set('year', String(year.value))
  if (department.value) params.set('department', department.value)
  return params.toString()
})
function message(exc: unknown) {
  const detail = (exc as { payload?: { detail?: string | {message?: string} } })?.payload?.detail
  if (typeof detail === 'string') return detail
  return detail?.message || (exc instanceof Error ? exc.message : String(exc))
}
async function load() {
  if (disposed || maintenanceActive.value) return
  if (loading.value) { pendingReload = true; return }
  loading.value = true
  clearTimeout(timer)
  try {
    const [summary, queue] = await Promise.all([getMirrorOverview(), getMirrorGaps(query.value)])
    if (disposed) return
    overview.value = summary; gaps.value = queue.items; total.value = queue.total
    const audit = summary.audit
    if (audit) {
      const params = new URLSearchParams({ page: String(findingPage.value), bucket: bucket.value })
      if (!bucket.value) params.delete('bucket')
      const rows = await getMirrorFindings(audit.id, params.toString())
      findings.value = rows.items; findingTotal.value = rows.total
    }
    if (storedJob.value) {
      job.value = await getMirrorJob(storedJob.value)
      const rows = await getMirrorAttempts(storedJob.value, attemptPage.value)
      attempts.value = rows.items; attemptTotal.value = rows.total
    }
    error.value = ''
  } catch (exc) { error.value = message(exc) }
  finally {
    loading.value = false
    if (pendingReload && !disposed) { pendingReload = false; void load(); return }
    if (!disposed && busy.value) timer = setTimeout(() => void load(), 2000)
  }
}
async function act(path: string, body: unknown = {}) {
  submitting.value = true; error.value = ''; notice.value = ''
  try {
    const result = await mirrorAction<{ job_id?: string | null; status: string }>(path, body)
    if (result.job_id && path === 'backfill') {
      storedJob.value = result.job_id; localStorage.setItem('sbpeye-mirror-job', result.job_id); attemptPage.value = 1
    }
    notice.value = result.status === 'empty' ? 'No eligible gaps match these controls.' : result.status === 'cancelling' ? 'Cancellation requested. Active requests are finishing.' : ''
    await load()
  } catch (exc) { error.value = message(exc) }
  finally { submitting.value = false }
}
function startBackfill() {
  return act('backfill', { size: size.value, workers: workers.value, delay: delay.value, max_attempts: maxAttempts.value,
    include_attachments: attachments.value, repeat_until_done: repeat.value, llm_features: selectedFeatures.value,
    statuses: includeFailed.value ? ['pending', 'failed'] : ['pending'], years: year.value ? [year.value] : [],
    departments: department.value ? [department.value] : [], order: order.value })
}
watch([status, q, year, department], () => { page.value = 1; void load() })
watch(bucket, () => { findingPage.value = 1; void load() })
watch([page, findingPage, attemptPage], () => void load())
onMounted(load)
onUnmounted(() => { disposed = true; clearTimeout(timer) })
</script>

<template>
  <div class="mirror">
    <IdentityMigrationPanel @maintenance="maintenanceChanged" />
    <template v-if="!maintenanceActive">
    <Message v-if="error" severity="error" :closable="false">{{ error }} <Button label="Retry" text @click="load" /></Message>
    <Message v-if="notice" severity="info" :closable="false">{{ notice }}</Message>
    <Card><template #title>Live listing coverage</template><template #content>
      <p v-if="!baseline">No complete assessment yet.</p>
      <template v-else>
        <p>Last complete audit: {{ formatDate(baseline.started_at) }} · {{ formatCount(baseline.distinct_total) }} distinct circular identities from {{ formatCount(baseline.raw_total) }} listing entries.</p>
        <div class="metrics"><span v-for="(count, label) in baseline.counts" :key="label">{{ String(label).replace(/_/g, ' ') }}: <strong>{{ formatCount(count) }}</strong></span></div>
        <p>Coverage records circular presence. Search readiness and attachments are reported separately. Duplicate listings are normal; ambiguous identities need review.</p>
        <details><summary>Coverage by year</summary><table><thead><tr><th>Year</th><th>Matched</th><th>URL matches</th><th>Missing</th><th>Ambiguous</th></tr></thead><tbody><tr v-for="(counts, label) in baseline.coverage" :key="label"><td>{{ label }}</td><td>{{ counts.matched || 0 }}</td><td>{{ counts.drifted || 0 }}</td><td>{{ counts.missing || 0 }}</td><td>{{ counts.ambiguous || 0 }}</td></tr></tbody></table></details>
      </template>
      <p v-if="overview?.audit">Latest attempt: <AdminStatusChip :status="overview.audit.status" /> {{ overview.audit.error_code }} {{ overview.audit.error }}</p>
      <Message v-if="overview?.audit && ['partial', 'failed'].includes(overview.audit.status)" severity="warn" :closable="false">Observed missing listings were added to the queue and can be backfilled. This incomplete attempt did not reconcile existing gaps: observed gaps can be understated and unlisted local rows inflated.</Message>
      <details v-if="overview?.audit?.diagnostics?.length"><summary>Page diagnostics</summary><ul><li v-for="item in overview.audit.diagnostics" :key="item.page">Page {{ item.page + 1 }}: {{ item.error || `${item.yield} entries` }}</li></ul></details>
      <Button label="Run full audit" :disabled="busy" @click="act('audit', { workers: workers, delay: delay })" />
      <Button label="Refresh" text :loading="loading" @click="load" />
    </template></Card>

    <Card><template #title>Audit findings</template><template #content>
      <label>Finding <select v-model="bucket"><option value="">All</option><option v-for="value in ['matched','drifted','missing','ambiguous','unlisted_local']" :key="value">{{ value }}</option></select></label>
      <div class="table-scroll"><table><thead><tr><th>Circular</th><th>Finding</th><th>Evidence</th></tr></thead><tbody><tr v-for="item in findings" :key="item.item_key"><td>{{ item.descriptor.reference }}<br>{{ item.descriptor.title }}</td><td>{{ item.bucket }}</td><td><details><summary>{{ item.variants.length }} listing variants</summary><ul><li v-for="variant in item.variants" :key="variant.url"><a :href="variant.url" target="_blank" rel="noopener">{{ variant.url }}</a></li></ul><pre>{{ item.evidence }}</pre></details></td></tr></tbody></table></div>
      <div class="actions"><Button label="Previous" text :disabled="findingPage <= 1" @click="findingPage--" /><span>{{ findingPage }} · {{ findingTotal }} findings</span><Button label="Next" text :disabled="findingPage * 50 >= findingTotal" @click="findingPage++" /></div>
    </template></Card>

    <Card><template #title>Current gap queue</template><template #content>
      <div class="metrics"><span v-for="(count, label) in overview?.queue_counts" :key="label">{{ label }}: <strong>{{ count }}</strong></span></div>
      <p>These live counts change as jobs finish. The audit above remains a historical snapshot.</p>
      <div class="controls"><label>Status<select v-model="status"><option value="">All</option><option v-for="value in ['pending','failed','resolved','skipped','running']" :key="value">{{ value }}</option></select></label><label>Search<InputText v-model="q" maxlength="200" /></label><label>Year<InputNumber v-model="year" :min="1000" :max="9999" :use-grouping="false" /></label><label>Department<InputText v-model="department" maxlength="100" /></label></div>
      <a :href="`/api/admin/mirror/gaps.csv?${query}`">Export filtered queue as CSV</a>
      <label class="reason">Reason for skipping<InputText v-model="skipReason" maxlength="1000" /></label>
      <div class="table-scroll"><table><thead><tr><th>Circular</th><th>Status</th><th>Attempts / issue</th><th>Actions</th></tr></thead><tbody><tr v-for="gap in gaps" :key="gap.id"><td>{{ gap.descriptor.reference }}<br>{{ gap.descriptor.title }}</td><td><AdminStatusChip :status="gap.status" /><p v-if="!gap.eligible">Held: {{ gap.eligibility_reason }}</p></td><td>{{ gap.attempts }}<p>{{ gap.last_error }}</p></td><td><Button v-if="['pending','failed'].includes(gap.status)" label="Skip" text :disabled="busy || !skipReason.trim()" @click="act(`gaps/${gap.id}/skip`, {reason: skipReason})" /><Button v-if="['failed','skipped'].includes(gap.status)" label="Requeue" text :disabled="busy" @click="act(`gaps/${gap.id}/requeue`)" /></td></tr></tbody></table></div>
      <div class="actions"><Button label="Previous" text :disabled="page <= 1" @click="page--" /><span>{{ page }} · {{ total }} gaps</span><Button label="Next" text :disabled="page * 50 >= total" @click="page++" /></div>
    </template></Card>

    <Card><template #title>Backfill controls</template><template #content>
      <p>Uses the year and department above. Selects pending gaps by default; text and queue-status filters affect the table only.</p>
      <div class="controls"><label>Batch size<InputNumber v-model="size" :min="1" :max="500" /></label><label>Workers<InputNumber v-model="workers" :min="1" :max="8" /></label><label>Request spacing (seconds)<InputNumber v-model="delay" :min="0" :max="10" :max-fraction-digits="2" /></label><label>Failure limit<InputNumber v-model="maxAttempts" :min="1" :max="20" /></label><label>Order<select v-model="order"><option value="newest_first">Newest first</option><option value="oldest_first">Oldest first</option><option value="fewest_attempts">Fewest attempts</option></select></label></div>
      <div class="checks"><label><Checkbox v-model="attachments" binary /> Fetch attachments</label><label><Checkbox v-model="includeFailed" binary /> Include failed gaps</label><label><Checkbox v-model="repeat" binary /> Process all currently eligible gaps in batches</label></div>
      <p>Attachment fetching is off by default. Fetched attachments are indexed automatically. Deferred attachments can be fetched and indexed in a later maintenance pass.</p>
      <fieldset><legend>Generate missing AI analyses</legend><div class="checks"><label v-for="feature in features" :key="feature"><Checkbox v-model="selectedFeatures" :value="feature" /> {{ feature }}</label></div></fieldset>
      <Button label="Start backfill" :disabled="busy" :loading="submitting" @click="startBackfill" />
    </template></Card>

    <Card v-if="job"><template #title>Backfill progress</template><template #content>
      <AdminStatusChip :status="job.status" /><p v-if="job.error_count">Completed items include {{ job.error_count }} required-stage errors. Stored circulars remain resolved; index repair uses stored text.</p><p>{{ job.error }}</p>
      <div class="metrics"><span v-for="(value, key) in job.progress" :key="key">{{ String(key).replace(/_/g, ' ') }}: {{ typeof value === 'number' ? Math.round(value) : value }}</span></div>
      <Button v-if="active(job.status)" label="Cancel after active requests finish" severity="secondary" :disabled="submitting || Boolean(job.progress.cancel_requested)" @click="act(`backfill/${job.job_id}/cancel`)" />
      <details open><summary>Item outcomes</summary><ul><li v-for="attempt in attempts" :key="attempt.id">{{ attempt.descriptor.descriptor?.reference }} · {{ attempt.outcome }}<p>{{ attempt.error }}</p><pre>{{ attempt.stages }}</pre></li></ul></details>
      <div class="actions"><Button label="Previous" text :disabled="attemptPage <= 1" @click="attemptPage--" /><span>{{ attemptPage }} · {{ attemptTotal }} attempts</span><Button label="Next" text :disabled="attemptPage * 50 >= attemptTotal" @click="attemptPage++" /></div>
    </template></Card>
    </template>
  </div>
</template>

<style scoped>
.mirror { display: grid; gap: 1rem; min-width: 0; }
.mirror > * { min-width: 0; }
.mirror :deep(.p-card-content) { min-width: 0; overflow-wrap: anywhere; }
.mirror :deep(.p-inputnumber), .mirror :deep(.p-inputtext) { width: 100%; min-width: 0; }
.metrics, .actions, .checks { display: flex; gap: 1rem; flex-wrap: wrap; align-items: center; margin: 1rem 0; }
.controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); gap: 1rem; margin: 1rem 0; }
label { display: flex; flex-direction: column; gap: .4rem; }
.checks label { flex-direction: row; align-items: center; }
.reason { margin: 1rem 0; max-width: 30rem; }
select { padding: .6rem; border: 1px solid var(--p-content-border-color); border-radius: .4rem; background: var(--p-content-background); color: var(--p-text-color); }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; text-align: left; }
th, td { padding: .65rem; border-bottom: 1px solid var(--p-content-border-color); vertical-align: top; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: .8rem; }
fieldset { border: 1px solid var(--p-content-border-color); margin-bottom: 1rem; }
</style>
