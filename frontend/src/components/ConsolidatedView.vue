<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import {
  getAIGenerationJob,
  getCircularConsolidation,
  startCircularGeneration,
  type AIGenerationJob,
  type ApiError,
  type ConsolidatedRequirement,
  type ConsolidationChainMember,
  type ConsolidationResponse,
} from '@/lib/api'

const props = defineProps<{ circularId: string }>()
const emit = defineEmits<{ navigate: [id: string] }>()

const payload = ref<ConsolidationResponse | null>(null)
const loading = ref(false)
const errorMessage = ref('')
const activeJob = ref<AIGenerationJob | null>(null)
let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollEpoch = 0

const chainById = computed(() => {
  const map = new Map<string, ConsolidationChainMember>()
  for (const member of payload.value?.chain ?? []) map.set(member.id, member)
  return map
})

const consolidation = computed(() => payload.value?.consolidation ?? null)

interface RequirementGroup {
  section: string
  items: ConsolidatedRequirement[]
}

const activeGroups = computed<RequirementGroup[]>(() => {
  const groups = new Map<string, ConsolidatedRequirement[]>()
  for (const item of consolidation.value?.requirements ?? []) {
    if (item.status === 'removed') continue
    const section = item.section || 'General'
    const list = groups.get(section) ?? []
    list.push(item)
    groups.set(section, list)
  }
  return [...groups.entries()].map(([section, items]) => ({ section, items }))
})

const removedItems = computed(() =>
  (consolidation.value?.requirements ?? []).filter((item) => item.status === 'removed'),
)

function memberLabel(id?: string | null): string {
  if (!id) return ''
  const member = chainById.value.get(id)
  return member?.reference || member?.title || 'Unindexed circular'
}

function memberDate(id?: string | null): string {
  if (!id) return ''
  return formatDate(chainById.value.get(id)?.date)
}

function formatDate(value?: string | null): string {
  if (!value) return ''
  return new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: '2-digit' }).format(new Date(value))
}

interface TextSegment {
  text: string
  mark: boolean
}

/** Split the requirement text so its key value renders highlighted. */
function segments(item: ConsolidatedRequirement): TextSegment[] {
  const text = item.text
  const value = item.value?.trim()
  if (!value) return [{ text, mark: false }]
  const index = text.toLowerCase().indexOf(value.toLowerCase())
  if (index === -1) return [{ text, mark: false }]
  return [
    { text: text.slice(0, index), mark: false },
    { text: text.slice(index, index + value.length), mark: true },
    { text: text.slice(index + value.length), mark: false },
  ].filter((segment) => segment.text.length > 0)
}

function stopPolling() {
  pollEpoch += 1
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
}

async function load() {
  loading.value = true
  errorMessage.value = ''
  try {
    payload.value = await getCircularConsolidation(props.circularId)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'Unable to load the consolidated view.'
  } finally {
    loading.value = false
  }
}

async function pollGeneration(jobId: string, epoch: number) {
  if (epoch !== pollEpoch) return
  try {
    const job = await getAIGenerationJob(jobId)
    if (epoch !== pollEpoch) return
    activeJob.value = job
    if (job.status === 'succeeded') {
      activeJob.value = null
      await load()
      return
    }
    if (job.status === 'failed') {
      activeJob.value = null
      errorMessage.value = job.error || 'Consolidation generation failed.'
      return
    }
    pollTimer = setTimeout(() => void pollGeneration(jobId, epoch), 1000)
  } catch (error) {
    activeJob.value = null
    errorMessage.value = error instanceof Error ? error.message : 'Unable to check generation status.'
  }
}

async function generate() {
  if (activeJob.value) return
  stopPolling()
  errorMessage.value = ''
  const epoch = pollEpoch
  try {
    const job = await startCircularGeneration(props.circularId, 'consolidation')
    activeJob.value = job
    void pollGeneration(job.id, epoch)
  } catch (error) {
    const apiError = error as ApiError
    const existingJob = apiError.payload?.job as AIGenerationJob | undefined
    if (apiError.status === 409 && existingJob?.id) {
      activeJob.value = existingJob
      void pollGeneration(existingJob.id, epoch)
      return
    }
    errorMessage.value = error instanceof Error ? error.message : 'Unable to start consolidation.'
  }
}

onMounted(load)
onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="consolidated-view">
    <div v-if="loading" class="consolidated-loading">
      <ProgressSpinner aria-label="Loading consolidated view" />
      <span>Loading consolidated view</span>
    </div>

    <template v-else-if="payload">
      <Message v-if="errorMessage" severity="error" :closable="false">{{ errorMessage }}</Message>

      <Message v-if="!payload.available" severity="info" :closable="false">
        This circular is not part of a resolved amendment chain.
      </Message>

      <template v-else>
        <div class="chain-timeline" role="list" aria-label="Amendment chain">
          <template v-for="(member, index) in payload.chain" :key="member.id">
            <i v-if="index > 0" class="pi pi-arrow-right chain-arrow" aria-hidden="true" />
            <button
              type="button"
              class="chain-chip"
              :class="{ current: member.id === props.circularId }"
              role="listitem"
              :title="member.title || undefined"
              @click="emit('navigate', member.id)"
            >
              <span class="chain-chip-ref">{{ member.reference || member.title }}</span>
              <span v-if="member.date" class="chain-chip-date">{{ formatDate(member.date) }}</span>
            </button>
          </template>
        </div>

        <Message v-if="payload.has_attachments" severity="warn" :closable="false">
          Some circulars in this chain have attachments; requirements inside attached
          documents are not part of this consolidation.
        </Message>

        <div v-if="activeJob" class="consolidated-progress" role="status">
          <ProgressSpinner style="width: 1.2rem; height: 1.2rem" />
          <span>
            Consolidating the amendment chain
            <template v-if="activeJob.progress_total">
              ({{ activeJob.progress_completed }}/{{ activeJob.progress_total }} circulars)
            </template>
          </span>
        </div>

        <div v-else-if="!consolidation" class="consolidated-empty">
          <i class="pi pi-sparkles" />
          <p>No consolidated view has been generated for this chain yet.</p>
          <Button icon="pi pi-sparkles" label="Generate consolidated view" size="small" @click="generate" />
        </div>

        <template v-else>
          <div class="consolidated-header">
            <div class="consolidated-asof">
              Consolidated as of
              <button type="button" class="asof-link" @click="emit('navigate', consolidation.as_of_circular_id)">
                {{ memberLabel(consolidation.as_of_circular_id) }}
              </button>
              <span v-if="memberDate(consolidation.as_of_circular_id)">
                ({{ memberDate(consolidation.as_of_circular_id) }})
              </span>
            </div>
            <Button
              icon="pi pi-refresh"
              :label="consolidation.stale ? 'Regenerate (stale)' : 'Regenerate'"
              text
              size="small"
              @click="generate"
            />
          </div>

          <Message v-if="consolidation.stale" severity="warn" :closable="false">
            The chain changed after this consolidation was generated — a newer amendment
            may be missing. Regenerate to bring it up to date.
          </Message>

          <section v-for="group in activeGroups" :key="group.section" class="requirement-group">
            <h3>{{ group.section }}</h3>
            <ul class="requirement-list">
              <li
                v-for="item in group.items"
                :key="item.req_id"
                class="requirement-item"
                :class="`req-${item.status}`"
              >
                <p class="requirement-text">
                  <template v-for="(segment, index) in segments(item)" :key="index">
                    <mark v-if="segment.mark" :class="{ changed: item.status === 'modified' }">{{ segment.text }}</mark>
                    <template v-else>{{ segment.text }}</template>
                  </template>
                </p>
                <div class="requirement-meta">
                  <span v-if="item.status === 'modified' && item.old_value" class="old-value">
                    was <s>{{ item.old_value }}</s>
                  </span>
                  <button
                    v-if="item.status === 'modified' && item.last_changed_by"
                    type="button"
                    class="provenance-chip"
                    @click="emit('navigate', item.last_changed_by!)"
                  >
                    <i class="pi pi-pencil" /> per {{ memberLabel(item.last_changed_by) }}
                  </button>
                  <button
                    v-else-if="item.status === 'added'"
                    type="button"
                    class="provenance-chip added-chip"
                    @click="emit('navigate', item.introduced_by)"
                  >
                    <i class="pi pi-plus" /> added by {{ memberLabel(item.introduced_by) }}
                  </button>
                  <span v-if="item.applies_to" class="applies-to">{{ item.applies_to }}</span>
                  <span
                    v-if="item.confidence === 'low'"
                    class="low-confidence"
                    title="The stated value could not be verified verbatim against the source circular."
                  >
                    <i class="pi pi-exclamation-triangle" /> unverified
                  </span>
                </div>
              </li>
            </ul>
          </section>

          <section v-if="removedItems.length" class="requirement-group removed-group">
            <h3>No longer in force</h3>
            <ul class="requirement-list">
              <li v-for="item in removedItems" :key="item.req_id" class="requirement-item req-removed">
                <p class="requirement-text"><s>{{ item.text }}</s></p>
                <div class="requirement-meta">
                  <button
                    v-if="item.removed_by"
                    type="button"
                    class="provenance-chip removed-chip"
                    @click="emit('navigate', item.removed_by!)"
                  >
                    <i class="pi pi-minus" /> removed by {{ memberLabel(item.removed_by) }}
                  </button>
                </div>
              </li>
            </ul>
          </section>

          <p v-if="consolidation.generated_at" class="consolidated-footnote">
            Generated {{ formatDate(consolidation.generated_at) }}<template v-if="consolidation.model"> · {{ consolidation.model }}</template>
          </p>
        </template>
      </template>
    </template>
  </div>
</template>

<style scoped>
.consolidated-view {
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
  padding: 1rem 1.25rem 1.25rem;
  overflow-y: auto;
  height: 100%;
}

.consolidated-loading,
.consolidated-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.6rem;
  padding: 2.5rem 1rem;
  color: var(--p-text-muted-color, #6b7280);
}

.consolidated-empty i {
  font-size: 1.4rem;
}

.chain-timeline {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem;
}

.chain-arrow {
  font-size: 0.65rem;
  color: var(--p-text-muted-color, #9ca3af);
}

.chain-chip {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.05rem;
  padding: 0.3rem 0.6rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.5rem;
  background: transparent;
  cursor: pointer;
  font: inherit;
  text-align: left;
}

.chain-chip:hover {
  border-color: var(--p-primary-color, #14532d);
}

.chain-chip.current {
  border-color: var(--p-primary-color, #14532d);
  background: color-mix(in srgb, var(--p-primary-color, #14532d) 8%, transparent);
}

.chain-chip-ref {
  font-size: 0.72rem;
  font-weight: 600;
}

.chain-chip-date {
  font-size: 0.64rem;
  color: var(--p-text-muted-color, #6b7280);
}

.consolidated-progress {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.8rem;
  color: var(--p-text-muted-color, #6b7280);
}

.consolidated-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.6rem;
  flex-wrap: wrap;
}

.consolidated-asof {
  font-size: 0.8rem;
  color: var(--p-text-muted-color, #6b7280);
}

.asof-link {
  border: none;
  background: none;
  padding: 0;
  font: inherit;
  font-weight: 600;
  color: var(--p-primary-color, #14532d);
  cursor: pointer;
}

.requirement-group h3 {
  margin: 0.4rem 0 0.3rem;
  font-size: 0.72rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--p-text-muted-color, #6b7280);
}

.requirement-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}

.requirement-item {
  padding: 0.55rem 0.6rem;
  border-top: 1px solid var(--p-content-border-color, #e5e7eb);
  border-left: 3px solid transparent;
}

.requirement-item.req-modified {
  border-left-color: var(--p-amber-500, #f59e0b);
  background: color-mix(in srgb, var(--p-amber-500, #f59e0b) 5%, transparent);
}

.requirement-item.req-added {
  border-left-color: var(--p-green-500, #22c55e);
  background: color-mix(in srgb, var(--p-green-500, #22c55e) 5%, transparent);
}

.requirement-item.req-removed {
  border-left-color: var(--p-red-400, #f87171);
  color: var(--p-text-muted-color, #6b7280);
}

.requirement-text {
  margin: 0;
  font-size: 0.82rem;
  line-height: 1.45;
}

.requirement-text mark {
  background: transparent;
  color: inherit;
  font-weight: 600;
}

.requirement-text mark.changed {
  background: color-mix(in srgb, var(--p-amber-500, #f59e0b) 25%, transparent);
  border-radius: 0.2rem;
  padding: 0 0.15rem;
}

.requirement-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.3rem 0.6rem;
  margin-top: 0.25rem;
  font-size: 0.7rem;
}

.old-value {
  color: var(--p-text-muted-color, #6b7280);
}

.provenance-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  padding: 0.1rem 0.5rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 999px;
  background: transparent;
  font: inherit;
  font-size: 0.68rem;
  cursor: pointer;
  color: var(--p-amber-700, #b45309);
}

.provenance-chip:hover {
  border-color: currentColor;
}

.provenance-chip.added-chip {
  color: var(--p-green-700, #15803d);
}

.provenance-chip.removed-chip {
  color: var(--p-red-600, #dc2626);
}

.applies-to {
  color: var(--p-text-muted-color, #6b7280);
}

.low-confidence {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  color: var(--p-amber-700, #b45309);
}

.removed-group {
  opacity: 0.85;
}

.consolidated-footnote {
  margin: 0.4rem 0 0;
  font-size: 0.66rem;
  color: var(--p-text-muted-color, #9ca3af);
}
</style>
