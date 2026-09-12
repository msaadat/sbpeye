<script setup lang="ts">
/**
 * Library — admin-uploaded documents in the laws corpus (docs/LAWS_UPLOADS_PLAN.md).
 *
 * The suggested targets sit above the form rather than below the table, because they are
 * the reason this tab exists: some rows in the corpus are Acts SBP lists and does not host
 * (the Banking Companies Ordinance lives on pakistancode.gov.pk), each already carrying
 * its backlinks and its place in the name index, each holding no text. Uploading text
 * against one of those is the common case; a wholly unlisted law is the other.
 *
 * The form resolves its title before committing, so attaching to SBP's existing row is a
 * choice rather than a surprise.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'
import ToggleSwitch from 'primevue/toggleswitch'

import {
  getLawUploads,
  pinLawVersion,
  resolveLawUpload,
  restoreLaw,
  unpinLawVersion,
  uploadLaw,
  withdrawLaw,
  type LawUploadRow,
  type LawUploadTarget,
  type LawVersion,
} from '@/lib/api'
import { formatDate } from './adminFormat'

const toast = useToast()
const route = useRoute()
const router = useRouter()

const DOC_TYPES = [
  { label: 'Law', value: 'law' },
  { label: 'Regulation', value: 'regulation' },
  { label: 'Guideline', value: 'guideline' },
  { label: 'Gazette', value: 'gazette' },
  { label: 'Licensing', value: 'licensing' },
]

const rows = ref<LawUploadRow[]>([])
const loading = ref(false)
const loadError = ref('')
/** Which document's version timeline is open. One at a time keeps the table readable. */
const expandedId = ref<string | null>(null)
const busyId = ref<string | null>(null)

const file = ref<File | null>(null)
const title = ref('')
const docType = ref('law')
const documentId = ref<string | null>(null)
const sourceUrl = ref('')
const sourceNote = ref('')
const versionLabel = ref('')
const effectiveFrom = ref('')
const pin = ref(false)
const submitting = ref(false)

const target = ref<LawUploadTarget | null>(null)
const resolving = ref(false)
const resolveError = ref('')

const suggested = computed(() =>
  rows.value.filter((row) => row.is_external && !row.current_version),
)
const held = computed(() => rows.value.filter((row) => !suggested.value.includes(row)))

const canSubmit = computed(() => Boolean(file.value && title.value.trim() && !submitting.value))

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    rows.value = await getLawUploads()
  } catch (error) {
    loadError.value = (error as Error).message || 'Could not load the library.'
  } finally {
    loading.value = false
  }
}

/**
 * Preview where this title lands.
 *
 * Debounced, because it fires as the admin types and every keystroke is a round trip.
 * An explicit `documentId` (set by "Add its text" on a suggested row) overrides the
 * title, for the case where their wording differs from SBP's.
 */
let resolveTimer: ReturnType<typeof setTimeout> | undefined
watch([title, documentId], () => {
  clearTimeout(resolveTimer)
  target.value = null
  resolveError.value = ''
  const value = title.value.trim()
  if (!value) return
  resolving.value = true
  resolveTimer = setTimeout(async () => {
    try {
      target.value = await resolveLawUpload(value, documentId.value)
    } catch (error) {
      resolveError.value = (error as Error).message || 'Could not resolve this title.'
    } finally {
      resolving.value = false
    }
  }, 350)
})

function pickFile(event: Event): void {
  const input = event.target as HTMLInputElement
  file.value = input.files?.[0] ?? null
  // A filename is a decent first guess at a title, and it is easier to correct than to
  // type from scratch.
  if (file.value && !title.value.trim()) {
    title.value = file.value.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ').trim()
  }
}

function addTextFor(row: LawUploadRow): void {
  documentId.value = row.id
  // `display_title`, not `title`: SBP's suffixes are state, not name, and prefilling
  // "Banking Companies Ordinance 1962 (being updated)" would bake a passing condition
  // into the document's title — and resolve to a different identity if the admin then
  // cleared the explicit id.
  title.value = row.display_title
  docType.value = row.doc_type || 'law'
  sourceUrl.value = row.source_url || ''
  document.getElementById('library-upload-form')?.scrollIntoView({ behavior: 'smooth' })
}

function resetForm(): void {
  file.value = null
  title.value = ''
  documentId.value = null
  sourceUrl.value = ''
  sourceNote.value = ''
  versionLabel.value = ''
  effectiveFrom.value = ''
  pin.value = false
  target.value = null
  const input = document.getElementById('library-file') as HTMLInputElement | null
  if (input) input.value = ''
}

async function submit(): Promise<void> {
  if (!file.value) return
  submitting.value = true
  try {
    const result = await uploadLaw(file.value, {
      title: title.value.trim(),
      doc_type: docType.value,
      document_id: documentId.value,
      source_url: sourceUrl.value.trim() || null,
      source_note: sourceNote.value.trim() || null,
      version_label: versionLabel.value.trim() || null,
      effective_from: effectiveFrom.value.trim() || null,
      pin: pin.value,
    })

    if (result.duplicate) {
      toast.add({
        severity: 'info',
        summary: 'Already held',
        detail: 'These exact bytes are already an edition of this document. Nothing was stored.',
        life: 6000,
      })
    } else if (!result.will_be_searchable) {
      // The whole point of extracting synchronously: this is said now, not discovered
      // weeks later by someone whose search came back empty.
      toast.add({
        severity: 'warn',
        summary: 'Stored, but not searchable',
        detail: `No text could be read from this file (${result.extraction_status}). It is archived and readable, but it will not appear in search or chat.`,
        life: 10000,
      })
    } else {
      toast.add({
        severity: 'success',
        summary: result.created_document ? 'Document created' : 'Edition added',
        detail: result.is_current
          ? 'This is now the edition in force. Indexing is running in the background.'
          : "Captured, but SBP's own copy is still in force. Pin this edition to override it.",
        life: 8000,
      })
    }
    resetForm()
    await load()
  } catch (error) {
    toast.add({
      severity: 'error',
      summary: 'Upload failed',
      detail: (error as Error).message,
      life: 8000,
    })
  } finally {
    submitting.value = false
  }
}

async function act(row: LawUploadRow, action: () => Promise<unknown>, done: string): Promise<void> {
  busyId.value = row.id
  try {
    await action()
    toast.add({ severity: 'success', summary: done, detail: row.display_title, life: 4000 })
    await load()
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Failed', detail: (error as Error).message, life: 6000 })
  } finally {
    busyId.value = null
  }
}

function toggleWithdraw(row: LawUploadRow): void {
  if (row.delisted_at) {
    void act(row, () => restoreLaw(row.id), 'Restored')
    return
  }
  // Not a delete, and worth saying so: the row, its editions and its archived files all
  // stay, and restoring is one click.
  const ok = window.confirm(
    `Withdraw "${row.display_title}" from the corpus?\n\n` +
      'It disappears from the list, search and chat. Nothing is deleted and you can restore it.',
  )
  if (ok) void act(row, () => withdrawLaw(row.id), 'Withdrawn')
}

function togglePin(row: LawUploadRow, version: LawVersion): void {
  void act(
    row,
    () =>
      version.pinned
        ? unpinLawVersion(row.id, version.id)
        : pinLawVersion(row.id, version.id),
    version.pinned ? 'Unpinned' : 'Pinned',
  )
}

function stateOf(row: LawUploadRow): string {
  if (row.delisted_at) return 'Withdrawn'
  const current = row.current_version
  if (!current) return 'No text held'
  if (current.extraction_status !== 'extracted') return `Not searchable (${current.extraction_status})`
  return current.pinned ? "Pinned over SBP's copy" : 'In force'
}

function versionLabelOf(version: LawVersion): string {
  const source = version.source === 'upload' ? 'Uploaded' : version.source === 'live' ? 'From SBP' : version.source
  const parts = [source]
  if (version.version_label) parts.push(version.version_label)
  if (version.pending) parts.push('not yet in force')
  return parts.join(' · ')
}

onMounted(async () => {
  await load()
  // Deep link from the reader's "Add its text" button.
  const preselect = route.query.document_id
  if (typeof preselect === 'string' && preselect) {
    const row = rows.value.find((item) => item.id === preselect)
    if (row) addTextFor(row)
    void router.replace({ query: {} })
  }
})
</script>

<template>
  <div class="admin-tab-body">
    <Card v-if="suggested.length" class="glass-panel">
      <template #title>Listed by SBP, hosted elsewhere</template>
      <template #content>
        <p class="field-hint">
          SBP links these but does not host them, so we hold no text — they are in the list
          and in every circular's backlinks with nothing to read. Uploading a copy attaches
          it to the row SBP already gave us.
        </p>
        <DataTable :value="suggested" :loading="loading" size="small" class="facet-table">
          <Column header="Document">
            <template #body="{ data }">
              <span class="row-title">{{ data.display_title }}</span>
              <a
                v-if="data.source_url"
                class="row-source"
                :href="data.source_url"
                target="_blank"
                rel="noreferrer"
              >where SBP points</a>
            </template>
          </Column>
          <Column header="Type">
            <template #body="{ data }">{{ data.doc_type || '—' }}</template>
          </Column>
          <Column header="">
            <template #body="{ data }">
              <Button
                text
                size="small"
                icon="pi pi-upload"
                label="Add its text"
                @click="addTextFor(data)"
              />
            </template>
          </Column>
          <template #empty>Nothing awaiting text.</template>
        </DataTable>
      </template>
    </Card>

    <Card id="library-upload-form" class="glass-panel">
      <template #title>{{ documentId ? 'Add text to a listed document' : 'Upload a document' }}</template>
      <template #content>
        <p class="field-hint">
          PDF or plain text. Text is extracted here and now, so this form can tell you
          whether the document will actually be searchable rather than leaving it to be
          discovered later. Word files and spreadsheets are refused: the analysis pipeline
          cannot read them either.
        </p>

        <form class="upload-form" @submit.prevent="submit">
          <label class="field">
            <span>File</span>
            <input id="library-file" type="file" accept=".pdf,.txt" @change="pickFile" />
          </label>

          <label class="field wide">
            <span>Title</span>
            <InputText v-model="title" required autocomplete="off" placeholder="Banking Companies Ordinance, 1962" />
          </label>

          <label class="field">
            <span>Type</span>
            <Select v-model="docType" :options="DOC_TYPES" option-label="label" option-value="value" />
          </label>

          <label class="field wide">
            <span>Citation for this text</span>
            <Textarea
              v-model="sourceNote"
              rows="2"
              auto-resize
              placeholder="Consolidated text from pakistancode.gov.pk, as of March 2024"
            />
          </label>

          <label class="field wide">
            <span>Source URL <small>(optional)</small></span>
            <InputText v-model="sourceUrl" autocomplete="off" placeholder="https://pakistancode.gov.pk/…" />
          </label>

          <label class="field">
            <span>Edition label <small>(optional)</small></span>
            <InputText v-model="versionLabel" autocomplete="off" placeholder="Updated till March 2024" />
          </label>

          <label class="field">
            <span>In force from <small>(optional)</small></span>
            <InputText v-model="effectiveFrom" autocomplete="off" placeholder="2026-01-01" />
          </label>

          <label class="field inline-toggle">
            <ToggleSwitch v-model="pin" aria-label="Pin this edition over SBP's own copy" />
            <span>Pin over SBP's own copy</span>
          </label>

          <div class="form-actions">
            <Button type="submit" label="Upload" icon="pi pi-upload" :loading="submitting" :disabled="!canSubmit" />
            <Button type="button" text label="Clear" :disabled="submitting" @click="resetForm" />
          </div>
        </form>

        <Message v-if="resolveError" severity="error" :closable="false">{{ resolveError }}</Message>
        <p v-else-if="resolving" class="resolve-line is-muted">Resolving…</p>
        <p v-else-if="target && !target.exists" class="resolve-line">
          <i class="pi pi-plus-circle" />
          New document. Nothing in the corpus has this name.
        </p>
        <p v-else-if="target" class="resolve-line is-attach">
          <i class="pi pi-link" />
          Attaches to <strong>{{ target.title }}</strong> — {{ target.summary }}
        </p>
      </template>
    </Card>

    <Card class="glass-panel">
      <template #title>Uploaded documents</template>
      <template #content>
        <Message v-if="loadError" severity="error" :closable="false">{{ loadError }}</Message>

        <DataTable :value="held" :loading="loading" size="small" class="facet-table">
          <Column header="Document">
            <template #body="{ data }">
              <span class="row-title">{{ data.display_title }}</span>
              <span v-if="data.source_note" class="row-source">{{ data.source_note }}</span>
            </template>
          </Column>
          <Column header="Origin">
            <template #body="{ data }">
              {{ data.origin === 'upload' ? 'Uploaded' : 'SBP listing' }}
            </template>
          </Column>
          <Column header="State">
            <template #body="{ data }">
              <span :class="{ 'is-warn': stateOf(data).startsWith('Not searchable') }">
                {{ stateOf(data) }}
              </span>
            </template>
          </Column>
          <Column header="Editions">
            <template #body="{ data }">
              <Button
                text
                size="small"
                :label="`${data.version_count}`"
                :icon="expandedId === data.id ? 'pi pi-chevron-up' : 'pi pi-chevron-down'"
                @click="expandedId = expandedId === data.id ? null : data.id"
              />
            </template>
          </Column>
          <Column header="">
            <template #body="{ data }">
              <Button
                text
                size="small"
                :icon="data.delisted_at ? 'pi pi-undo' : 'pi pi-eye-slash'"
                :label="data.delisted_at ? 'Restore' : 'Withdraw'"
                :severity="data.delisted_at ? undefined : 'danger'"
                :loading="busyId === data.id"
                @click="toggleWithdraw(data)"
              />
            </template>
          </Column>
          <template #empty>Nothing uploaded yet.</template>
        </DataTable>

        <div v-if="expandedId" class="version-panel">
          <h3>Editions held</h3>
          <ul>
            <li
              v-for="version in held.find((row) => row.id === expandedId)?.versions || []"
              :key="version.id"
              :class="{ 'is-current': version.is_current }"
            >
              <div class="version-body">
                <span class="version-source">{{ versionLabelOf(version) }}</span>
                <span class="version-meta">
                  captured {{ formatDate(version.first_seen_at) }} ·
                  {{ version.extraction_status }}
                  <template v-if="version.original_filename"> · {{ version.original_filename }}</template>
                </span>
              </div>
              <span v-if="version.is_current" class="version-badge">In force</span>
              <Button
                v-if="version.source === 'upload'"
                text
                size="small"
                :icon="version.pinned ? 'pi pi-bookmark-fill' : 'pi pi-bookmark'"
                :label="version.pinned ? 'Unpin' : 'Pin'"
                :loading="busyId === expandedId"
                @click="togglePin(held.find((row) => row.id === expandedId)!, version)"
              />
            </li>
          </ul>
          <p class="field-hint">
            A pin makes an uploaded edition the one in force even when SBP hosts its own
            copy — the case for a scanned PDF with no text layer. Unpinning hands currency
            straight back to SBP's copy.
          </p>
        </div>
      </template>
    </Card>
  </div>
</template>

<style scoped>
.upload-form {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr));
  gap: 1rem;
  align-items: end;
  margin-bottom: 1rem;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
  font-size: 0.8125rem;
}

.field.wide {
  grid-column: 1 / -1;
}

.field small {
  opacity: 0.6;
  font-weight: 400;
}

.field.inline-toggle {
  flex-direction: row;
  align-items: center;
  gap: 0.5rem;
  padding-bottom: 0.5rem;
}

.form-actions {
  grid-column: 1 / -1;
  display: flex;
  gap: 0.5rem;
}

input[type='file'] {
  font-size: 0.8125rem;
  padding: 0.4rem 0;
}

.resolve-line {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.8125rem;
  margin: 0;
}

.resolve-line.is-muted,
.is-muted {
  opacity: 0.7;
}

.resolve-line.is-attach {
  color: var(--p-primary-color, #2563eb);
}

.row-title {
  display: block;
}

.row-source {
  display: block;
  font-size: 0.75rem;
  opacity: 0.65;
}

.is-warn {
  color: var(--p-orange-500, #f97316);
}

.version-panel {
  margin-top: 1rem;
  padding-top: 0.75rem;
  border-top: 1px solid var(--sbp-border);
}

.version-panel h3 {
  font-size: 0.8125rem;
  margin: 0 0 0.5rem;
}

.version-panel ul {
  list-style: none;
  margin: 0 0 0.75rem;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
}

.version-panel li {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.5rem 0.625rem;
  border: 1px solid var(--sbp-border);
  border-radius: 0.375rem;
}

.version-panel li.is-current {
  border-color: var(--p-primary-color, #2563eb);
}

.version-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.125rem;
}

.version-source {
  font-size: 0.8125rem;
}

.version-meta {
  font-size: 0.75rem;
  opacity: 0.65;
}

.version-badge {
  font-size: 0.6875rem;
  padding: 0.125rem 0.5rem;
  border-radius: 999px;
  background: var(--p-primary-color, #2563eb);
  color: #fff;
}
</style>
