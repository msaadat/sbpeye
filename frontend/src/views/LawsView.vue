<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import StateBlock from '@/components/StateBlock.vue'
import {
  buildLawFileUrl,
  getLawDetail,
  getLaws,
  getLawTypes,
  type LawDetail,
  type LawSummary,
  type LawTypeCount,
} from '@/lib/api'

const route = useRoute()
const router = useRouter()

/** The whole corpus, held client-side: 133 documents is small enough to browse, not search. */
const documents = ref<LawSummary[]>([])
const listLoading = ref(false)
const listError = ref('')

const detail = ref<LawDetail | null>(null)
const detailLoading = ref(false)
const detailError = ref('')

const query = ref('')
const matches = ref<LawSummary[] | null>(null)
const searching = ref(false)
const expanded = ref<Set<string>>(new Set())
const provenanceOpen = ref(false)

const corpusTypes = ref<LawTypeCount[]>([])
const typeFilter = ref('')

let searchTimer: ReturnType<typeof setTimeout> | null = null
let searchController: AbortController | null = null

const selectedId = computed(() => (route.params.id as string) || '')

const childrenByParent = computed(() => {
  const map = new Map<string, LawSummary[]>()
  for (const doc of documents.value) {
    if (!doc.parent_id) continue
    const bucket = map.get(doc.parent_id) || []
    bucket.push(doc)
    map.set(doc.parent_id, bucket)
  }
  // Chapter order, not alphabetical: a manual read A–Z is not a manual.
  for (const bucket of map.values()) {
    bucket.sort((a, b) => (a.part_order ?? Number.MAX_SAFE_INTEGER) - (b.part_order ?? Number.MAX_SAFE_INTEGER))
  }
  return map
})

const TYPE_ORDER = ['law', 'regulation', 'guideline']

function byAuthority(a: string, b: string): number {
  const ai = TYPE_ORDER.indexOf(a)
  const bi = TYPE_ORDER.indexOf(b)
  return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi)
}

/** Every group the tree can show, before the type filter narrows it. */
const allGroups = computed(() => {
  const buckets = new Map<string, LawSummary[]>()
  for (const doc of documents.value) {
    if (doc.parent_id) continue
    const key = doc.doc_type || 'other'
    const bucket = buckets.get(key) || []
    bucket.push(doc)
    buckets.set(key, bucket)
  }
  return [...buckets.entries()]
    .sort((a, b) => byAuthority(a[0], b[0]))
    .map(([doc_type, items]) => ({
      doc_type,
      items: items.sort((a, b) => a.display_title.localeCompare(b.display_title)),
    }))
})

const groups = computed(() =>
  typeFilter.value ? allGroups.value.filter((g) => g.doc_type === typeFilter.value) : allGroups.value,
)

/**
 * Filter chips. `/api/laws/types` decides which types exist — so a type we have not
 * hardcoded still gets a chip — but the count shown is the number of *rows the tree
 * will render*, i.e. top-level only. The API's own count spans the whole corpus and
 * would contradict what the user is looking at; it goes in the tooltip instead.
 */
const facets = computed(() => {
  const topLevel = new Map(allGroups.value.map((g) => [g.doc_type, g.items.length]))
  const known = corpusTypes.value.map((t) => t.doc_type)
  const names = [...new Set([...known, ...topLevel.keys()])].sort(byAuthority)
  return names
    .filter((name) => topLevel.get(name))
    .map((name) => ({
      doc_type: name,
      count: topLevel.get(name) || 0,
      corpusCount: corpusTypes.value.find((t) => t.doc_type === name)?.count ?? 0,
    }))
})

const topLevelTotal = computed(() => facets.value.reduce((sum, f) => sum + f.count, 0))
const visibleTotal = computed(() => groups.value.reduce((sum, g) => sum + g.items.length, 0))

/**
 * How much of each collection we actually hold. "26 parts" is SBP's claim; `held` is
 * ours, and the two differ — the Reporting Guidelines list 9 parts and every one of
 * them is a dead link, which a bare part count would hide.
 */
const holdingsById = computed(() => {
  const map = new Map<string, { parts: number; held: number }>()
  for (const [parentId, children] of childrenByParent.value) {
    map.set(parentId, {
      parts: children.length,
      held: children.filter((child) => child.current_version?.has_file).length,
    })
  }
  return map
})

const currentVersion = computed(() => detail.value?.current_version || null)

const partsHeld = computed(
  () => detail.value?.children.filter((child) => child.has_content).length || 0,
)

const fileUrl = computed(() => {
  const doc = detail.value
  if (!doc || !currentVersion.value?.has_file) return ''
  return buildLawFileUrl(doc.id, currentVersion.value.id)
})

const statusLine = computed(() => {
  const doc = detail.value
  if (!doc) return ''
  const parts: string[] = []
  // The suffix we stripped off the title belongs here — it was always state, never name.
  const edition = currentVersion.value?.version_label || doc.version_suffix
  if (edition) parts.push(capitalize(edition))
  if (doc.version_count > 1) parts.push(`${doc.version_count} editions held`)
  const captured = currentVersion.value?.first_seen_at
  if (captured) parts.push(`captured ${formatDate(captured)}`)
  return parts.join(' · ')
})

const custodyLine = computed(() => {
  const doc = detail.value
  if (!doc || !currentVersion.value) return ''
  const first = formatDate(currentVersion.value.first_seen_at)
  const last = formatDate(currentVersion.value.last_seen_at)
  if (doc.version_count > 1) {
    return `${doc.version_count} editions seen. The one in force was captured ${first}; last checked ${last}.`
  }
  return `One edition seen. First captured ${first}, and unchanged at the last check on ${last}.`
})

function formatDate(value?: string | null): string {
  if (!value) return 'an unknown date'
  return new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: '2-digit' })
    .format(new Date(value))
}

function capitalize(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1)
}

function typeLabel(value?: string | null): string {
  if (!value) return 'Document'
  return capitalize(value)
}

function isContainer(doc: LawSummary): boolean {
  return holdingsById.value.has(doc.id)
}

/** Some parts are titled with their own label ("NBFCs"); prefixing it reads as a stutter. */
function partPrefix(doc: LawSummary): string {
  const label = doc.part_label
  if (!label || label === doc.display_title) return ''
  return `${label} — `
}

/** The one muted line under a row: what we hold, and which edition of it. */
function subLine(doc: LawSummary): string {
  const held = holdingsById.value.get(doc.id)
  if (held) {
    const count = `${held.parts} part${held.parts === 1 ? '' : 's'}`
    if (!held.held) return `${count} · none held`
    if (held.held === held.parts) return `${count} · all held`
    return `${count} · ${held.held} held`
  }
  const edition = doc.current_version?.version_label || doc.version_suffix || ''
  const state = doc.is_external
    ? 'hosted externally'
    : doc.circular_id
      ? 'is a circular'
      : !doc.current_version
        ? 'no file held'
        : ''
  return [state, edition].filter(Boolean).join(' · ') || 'in force'
}

function toggle(doc: LawSummary) {
  const next = new Set(expanded.value)
  if (next.has(doc.id)) next.delete(doc.id)
  else next.add(doc.id)
  expanded.value = next
}

function select(doc: LawSummary) {
  if (isContainer(doc)) toggle(doc)
  if (doc.id === selectedId.value) return
  void router.push(`/laws/${encodeURIComponent(doc.id)}`)
}

async function loadCorpus() {
  listLoading.value = true
  listError.value = ''
  try {
    const collected: LawSummary[] = []
    for (let page = 1; page <= 5; page += 1) {
      const response = await getLaws({ page, per_page: 100 })
      collected.push(...response.items)
      if (collected.length >= response.total || response.items.length === 0) break
    }
    documents.value = collected
  } catch (error) {
    listError.value = (error as Error).message || 'Could not load the corpus'
  } finally {
    listLoading.value = false
  }
}

async function loadDetail(id: string) {
  if (!id) {
    detail.value = null
    return
  }
  detailLoading.value = true
  detailError.value = ''
  provenanceOpen.value = false
  try {
    detail.value = await getLawDetail(id)
    // Keep a part's container open so the reader is never shown without its context,
    // and open a container reached by deep link so its parts are there to pick from.
    const toOpen = detail.value.parent?.id || (detail.value.children.length ? detail.value.id : '')
    if (toOpen) {
      const next = new Set(expanded.value)
      next.add(toOpen)
      expanded.value = next
    }
  } catch (error) {
    detail.value = null
    detailError.value = (error as Error).message || 'Could not load this document'
  } finally {
    detailLoading.value = false
  }
}

function runSearch() {
  const value = query.value.trim()
  searchController?.abort()

  if (!value) {
    matches.value = null
    searching.value = false
    return
  }

  searching.value = true
  searchController = new AbortController()
  getLaws({ q: value, doc_type: typeFilter.value || undefined, per_page: 50 }, searchController.signal)
    .then((response) => {
      matches.value = response.items
    })
    .catch((error) => {
      if ((error as Error).name !== 'AbortError') matches.value = []
    })
    .finally(() => {
      searching.value = false
    })
}

watch(query, () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(runSearch, 250)
})

// The filter narrows the tree client-side, but a search runs server-side and has to be
// re-asked with the new type.
watch(typeFilter, () => {
  if (query.value.trim()) runSearch()
})

watch(selectedId, (id) => void loadDetail(id), { immediate: true })

onMounted(() => {
  void loadCorpus()
  // Facet source of truth; failing to load it just costs us the chips.
  getLawTypes()
    .then((types) => {
      corpusTypes.value = types
    })
    .catch(() => {
      corpusTypes.value = []
    })
})
</script>

<template>
  <div class="laws-view">
    <aside class="laws-library">
      <header class="library-head">
        <span class="library-title">Laws &amp; Regulations</span>
        <span class="library-count">{{ visibleTotal || '—' }}</span>
      </header>

      <div class="library-field">
        <i class="pi pi-search" />
        <input v-model="query" type="search" placeholder="Filter or search full text" />
      </div>

      <div v-if="facets.length > 1" class="library-facets">
        <button
          type="button"
          class="facet"
          :class="{ 'is-active': !typeFilter }"
          @click="typeFilter = ''"
        >All <span class="facet-count">{{ topLevelTotal }}</span></button>
        <button
          v-for="facet in facets"
          :key="facet.doc_type"
          type="button"
          class="facet"
          :class="{ 'is-active': typeFilter === facet.doc_type }"
          :title="`${facet.corpusCount} in the corpus, including parts`"
          :aria-label="`${typeLabel(facet.doc_type)}, ${facet.count} documents`"
          @click="typeFilter = typeFilter === facet.doc_type ? '' : facet.doc_type"
        >{{ typeLabel(facet.doc_type) }} <span class="facet-count">{{ facet.count }}</span></button>
      </div>

      <p v-if="listLoading" class="library-note">Loading the corpus…</p>
      <p v-else-if="listError" class="library-note is-error">{{ listError }}</p>

      <!-- Search results: flat, but a part still carries its container. -->
      <div v-else-if="matches" class="library-tree">
        <p class="library-note">
          {{ searching ? 'Searching…' : `${matches.length} match${matches.length === 1 ? '' : 'es'}` }}
        </p>
        <button
          v-for="item in matches"
          :key="item.id"
          type="button"
          class="node is-flat"
          :class="{ 'is-selected': item.id === selectedId }"
          @click="select(item)"
        >
          <span class="node-body">
            <span v-if="item.parent_id" class="node-crumb">
              {{ documents.find((d) => d.id === item.parent_id)?.display_title || 'Part of a collection' }}
            </span>
            <span class="node-title">{{ partPrefix(item) }}{{ item.display_title }}</span>
          </span>
        </button>
      </div>

      <div v-else class="library-tree">
        <template v-for="group in groups" :key="group.doc_type">
          <p class="group-label">{{ typeLabel(group.doc_type) }} · {{ group.items.length }}</p>
          <template v-for="doc in group.items" :key="doc.id">
            <button
              type="button"
              class="node"
              :class="{ 'is-selected': doc.id === selectedId }"
              @click="select(doc)"
            >
              <span class="node-caret">
                <i v-if="isContainer(doc)" class="pi" :class="expanded.has(doc.id) ? 'pi-chevron-down' : 'pi-chevron-right'" />
              </span>
              <span class="node-body">
                <span class="node-title">{{ doc.display_title }}</span>
                <span class="node-sub">{{ subLine(doc) }}</span>
                <!-- Only where the collection is incomplete: a full bar says nothing. -->
                <span
                  v-if="holdingsById.get(doc.id) && holdingsById.get(doc.id)!.held < holdingsById.get(doc.id)!.parts"
                  class="node-meter"
                  aria-hidden="true"
                >
                  <span
                    class="node-meter-fill"
                    :style="{ width: `${(holdingsById.get(doc.id)!.held / holdingsById.get(doc.id)!.parts) * 100}%` }"
                  />
                </span>
              </span>
            </button>

            <div v-if="isContainer(doc) && expanded.has(doc.id)" class="node-children">
              <button
                v-for="child in childrenByParent.get(doc.id)"
                :key="child.id"
                type="button"
                class="node is-child"
                :class="{ 'is-selected': child.id === selectedId }"
                @click="select(child)"
              >
                <span class="node-body">
                  <span class="node-title">
                    <span v-if="child.part_label !== child.display_title" class="node-part">{{ child.part_label }}</span>
                    {{ child.display_title }}
                  </span>
                  <!-- The container's meter says how many are missing; this says which. -->
                  <span v-if="!child.current_version?.has_file" class="node-sub">not held</span>
                </span>
              </button>
            </div>
          </template>
        </template>
      </div>
    </aside>

    <section class="laws-reader">
      <StateBlock
        v-if="detailLoading"
        state="loading"
        title="Opening the document"
      />
      <StateBlock
        v-else-if="detailError"
        state="error"
        title="Could not open this document"
        :message="detailError"
      />

      <template v-else-if="detail">
        <header class="reader-head">
          <div class="reader-identity">
            <p v-if="detail.parent" class="reader-crumb">{{ detail.parent.display_title }}</p>
            <h1 class="reader-title">
              <span v-if="detail.part_label && detail.part_label !== detail.display_title" class="reader-part">
                {{ detail.part_label }}
              </span>
              {{ detail.display_title }}
            </h1>
            <p class="reader-status">
              <span class="reader-tag">{{ typeLabel(detail.doc_type) }}</span>
              <span v-if="currentVersion" class="reader-force">In force</span>
              <span v-if="statusLine">{{ statusLine }}</span>
            </p>
          </div>
          <a
            v-if="fileUrl"
            class="reader-action"
            :href="fileUrl"
            target="_blank"
            rel="noreferrer"
          >Open in a new tab</a>
        </header>

        <!-- The archived file, straight from our disk. -->
        <iframe
          v-if="fileUrl"
          :key="fileUrl"
          class="reader-frame"
          :src="fileUrl"
          :title="detail.title"
        />

        <div v-else class="reader-empty">
          <template v-if="detail.circular_id">
            <h2>This entry is a circular.</h2>
            <p>SBP lists it among the regulations, but the document behind it lives with the circulars.</p>
            <RouterLink class="reader-action" :to="`/circulars/${detail.circular_id}`">Open the circular</RouterLink>
          </template>
          <template v-else-if="detail.is_external">
            <h2>Hosted outside SBP.</h2>
            <p>This one is published elsewhere, so we hold no copy of it — but the circulars that cite it are here.</p>
            <a v-if="detail.source_url" class="reader-action" :href="detail.source_url" target="_blank" rel="noreferrer">
              Open the external page
            </a>
          </template>
          <template v-else-if="detail.children.length">
            <h2>{{ detail.children.length }} parts.</h2>
            <p v-if="!partsHeld">
              This is a collection, but SBP's link is broken for every one of its parts, so we
              hold none of them. We retry on every sync.
            </p>
            <p v-else-if="partsHeld < detail.children.length">
              This is a collection. We hold {{ partsHeld }} of its {{ detail.children.length }} parts —
              SBP's link is broken for the rest. Pick a part on the left to read it.
            </p>
            <p v-else>This is a collection. Pick a part on the left to read it.</p>
          </template>
          <template v-else>
            <h2>SBP's link to this file is broken.</h2>
            <p>The listing points at a file we could not retrieve, so there is nothing archived to show. We retry on every sync.</p>
          </template>
        </div>

        <footer class="reader-provenance">
          <button type="button" class="provenance-bar" @click="provenanceOpen = !provenanceOpen">
            <span v-if="currentVersion">Held since {{ formatDate(currentVersion.first_seen_at) }}</span>
            <span v-else>Nothing archived</span>
            <span class="provenance-spacer" />
            <span>Cited by {{ detail.linked_circulars.length }} circular{{ detail.linked_circulars.length === 1 ? '' : 's' }}</span>
            <i class="pi" :class="provenanceOpen ? 'pi-chevron-down' : 'pi-chevron-up'" />
          </button>

          <div v-if="provenanceOpen" class="provenance-open">
            <div>
              <h3>Custody</h3>
              <p>{{ custodyLine || 'No file has been archived for this document.' }}</p>
            </div>
            <div>
              <h3>Cited by</h3>
              <p v-if="!detail.linked_circulars.length">No circular in the corpus cites this document.</p>
              <ul v-else>
                <li v-for="link in detail.linked_circulars.slice(0, 8)" :key="link.circular.id">
                  <RouterLink :to="`/circulars/${link.circular.id}`">
                    {{ link.circular.reference || link.circular.title }}
                  </RouterLink>
                </li>
              </ul>
            </div>
          </div>
        </footer>
      </template>

      <div v-else class="reader-overview">
        <h1>SBP's rulebook, as we hold it</h1>
        <p>
          {{ documents.length }} documents captured from sbp.org.pk. SBP replaces these files in place and
          keeps no history; from the day we started watching, we do. Pick anything on the left to open it.
        </p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.laws-view {
  display: grid;
  grid-template-columns: 300px 1fr;
  height: 100%;
  min-height: 0;
  overflow: hidden;
}

/* ---- Library ---- */
.laws-library {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  padding: 0.85rem 0.7rem 1rem;
  border-right: 1px solid var(--sbp-border);
  background: var(--sbp-bg);
  overflow-y: auto;
}

.library-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 0 0.25rem;
}

.library-title {
  font-size: 0.9rem;
  font-weight: 600;
  letter-spacing: -0.01em;
}

.library-count {
  font-size: 0.7rem;
  color: var(--sbp-muted);
  font-variant-numeric: tabular-nums;
}

.library-field {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.45rem 0.6rem;
  border: 1px solid var(--sbp-border);
  border-radius: var(--sbp-radius);
  background: var(--sbp-surface);
  color: var(--sbp-muted);
}

.library-field i {
  font-size: 0.78rem;
}

.library-field input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: none;
  background: transparent;
  color: var(--sbp-text);
  font: inherit;
  font-size: 0.8rem;
}

.library-facets {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
}

.facet {
  display: inline-flex;
  align-items: baseline;
  gap: 0.3rem;
  padding: 0.2rem 0.45rem;
  border: 1px solid transparent;
  border-radius: 999px;
  background: var(--sbp-subtle);
  color: var(--sbp-muted);
  font: inherit;
  font-size: 0.68rem;
  cursor: pointer;
  transition: color 0.16s var(--sbp-ease), border-color 0.16s var(--sbp-ease);
}

.facet:hover {
  color: var(--sbp-text);
}

.facet.is-active {
  border-color: color-mix(in srgb, var(--sbp-green) 40%, transparent);
  background: color-mix(in srgb, var(--sbp-green) 11%, var(--sbp-surface));
  color: var(--sbp-green);
  font-weight: 600;
}

.facet-count {
  font-size: 0.62rem;
  font-variant-numeric: tabular-nums;
  opacity: 0.75;
}

.library-note {
  margin: 0.3rem 0.35rem;
  font-size: 0.72rem;
  color: var(--sbp-muted);
}

.library-note.is-error {
  color: #a2453a;
}

.library-tree {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.group-label {
  margin: 0.75rem 0.35rem 0.3rem;
  font-size: 0.62rem;
  font-weight: 700;
  letter-spacing: 0.13em;
  text-transform: uppercase;
  color: var(--sbp-muted);
}

.node {
  display: grid;
  grid-template-columns: 0.85rem 1fr;
  gap: 0.4rem;
  align-items: start;
  width: 100%;
  padding: 0.38rem 0.4rem;
  border: 0;
  border-radius: var(--sbp-radius);
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background-color 0.16s var(--sbp-ease);
}

.node:hover {
  background: var(--sbp-subtle);
}

.node.is-selected {
  background: color-mix(in srgb, var(--sbp-green) 11%, var(--sbp-surface));
}

.node.is-selected .node-title {
  color: var(--sbp-green);
  font-weight: 600;
}

/* Rows with no caret slot: without this they land in the 0.85rem caret column. */
.node.is-child,
.node.is-flat {
  grid-template-columns: 1fr;
}

.node-caret {
  padding-top: 0.15rem;
  color: var(--sbp-muted);
  font-size: 0.6rem;
}

.node-body {
  min-width: 0;
}

.node-title {
  display: block;
  font-size: 0.79rem;
  line-height: 1.35;
}

.node-part {
  color: var(--sbp-muted);
  font-variant-numeric: tabular-nums;
}

.node-crumb {
  display: block;
  font-size: 0.66rem;
  color: var(--sbp-green);
  margin-bottom: 0.1rem;
}

.node-sub {
  display: block;
  margin-top: 0.1rem;
  font-size: 0.66rem;
  color: var(--sbp-muted);
}

/* Sits under an incomplete container's count: the gap between SBP's claim and our archive. */
.node-meter {
  display: block;
  height: 2px;
  margin-top: 0.28rem;
  border-radius: 1px;
  background: color-mix(in srgb, var(--sbp-muted) 22%, transparent);
  overflow: hidden;
}

.node-meter-fill {
  display: block;
  height: 100%;
  border-radius: 1px;
  background: color-mix(in srgb, var(--sbp-green) 55%, transparent);
}

.node-children {
  display: flex;
  flex-direction: column;
  gap: 1px;
  margin: 0.15rem 0 0.35rem 0.75rem;
  padding-left: 0.55rem;
  border-left: 1px solid var(--sbp-border);
}

/* ---- Reader ---- */
.laws-reader {
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--sbp-surface);
  overflow: hidden;
}

.reader-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1.2rem;
  padding: 0.85rem 1.2rem 0.75rem;
  border-bottom: 1px solid var(--sbp-border);
}

.reader-crumb {
  margin: 0 0 0.2rem;
  font-size: 0.72rem;
  color: var(--sbp-green);
  font-weight: 600;
}

.reader-title {
  margin: 0;
  font-size: 1.05rem;
  font-weight: 600;
  letter-spacing: -0.012em;
  line-height: 1.25;
}

.reader-part {
  color: var(--sbp-muted);
  font-weight: 500;
}

.reader-status {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.55rem;
  margin: 0.35rem 0 0;
  font-size: 0.74rem;
  color: var(--sbp-muted);
}

.reader-tag {
  padding: 0.12rem 0.4rem;
  border-radius: 3px;
  background: var(--sbp-subtle);
  font-size: 0.62rem;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.reader-force {
  color: var(--sbp-green);
  font-weight: 600;
}

.reader-action {
  flex: none;
  padding: 0.35rem 0.7rem;
  border: 1px solid var(--sbp-border);
  border-radius: var(--sbp-radius);
  background: var(--sbp-surface);
  color: var(--sbp-text);
  font-size: 0.74rem;
  white-space: nowrap;
}

.reader-action:hover {
  border-color: color-mix(in srgb, var(--sbp-green) 40%, var(--sbp-border));
  color: var(--sbp-green);
}

.reader-frame {
  flex: 1;
  width: 100%;
  min-height: 0;
  border: 0;
  background: var(--sbp-subtle);
}

.reader-empty,
.reader-overview {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  justify-content: center;
  gap: 0.5rem;
  padding: 2rem 2.4rem;
  max-width: 44rem;
}

.reader-empty h2,
.reader-overview h1 {
  margin: 0;
  font-size: 1.05rem;
  font-weight: 600;
}

.reader-empty p,
.reader-overview p {
  margin: 0;
  color: var(--sbp-muted);
  font-size: 0.85rem;
  line-height: 1.6;
}

.reader-empty .reader-action {
  margin-top: 0.5rem;
}

/* ---- Provenance ---- */
.reader-provenance {
  border-top: 1px solid var(--sbp-border);
  background: var(--sbp-surface);
}

.provenance-bar {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  width: 100%;
  padding: 0.55rem 1.2rem;
  border: 0;
  background: transparent;
  color: var(--sbp-muted);
  font: inherit;
  font-size: 0.74rem;
  text-align: left;
  cursor: pointer;
}

.provenance-bar:hover {
  color: var(--sbp-text);
}

.provenance-spacer {
  flex: 1;
}

.provenance-open {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1.6rem;
  padding: 0 1.2rem 1rem;
}

.provenance-open h3 {
  margin: 0 0 0.4rem;
  font-size: 0.62rem;
  font-weight: 700;
  letter-spacing: 0.13em;
  text-transform: uppercase;
  color: var(--sbp-muted);
}

.provenance-open p {
  margin: 0;
  font-size: 0.78rem;
  line-height: 1.6;
  color: var(--sbp-text);
}

.provenance-open ul {
  margin: 0;
  padding-left: 1rem;
  font-size: 0.78rem;
  line-height: 1.7;
}

.provenance-open a {
  color: var(--sbp-green);
}
</style>
