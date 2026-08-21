<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Popover from 'primevue/popover'
import StateBlock from '@/components/StateBlock.vue'
import RegulatoryValueList from '@/components/RegulatoryValueList.vue'
import RelationshipGroups, {
  type RelationGroup,
} from '@/components/RelationshipGroups.vue'
import SummarySection from '@/components/SummarySection.vue'
import { useResizablePane } from '@/lib/useResizablePane'
import { useAiGeneration } from '@/lib/useAiGeneration'
import { ADMIN_ONLY_EMPTY_HINT, adminOnlyHint, adminOnlyLabel } from '@/lib/adminOnly'
import { useCurrentUser } from '@/lib/useCurrentUser'
import {
  buildLawFileUrl,
  downloadLawChecklistExcel,
  getLawDetail,
  getLaws,
  getLawTypes,
  startLawGeneration,
  type LawDetail,
  type LawGenerationAction,
  type LawGenerationFeature,
  type LawRelationship,
  type LawSummary,
  type LawTypeCount,
} from '@/lib/api'

const route = useRoute()
const router = useRouter()
const toast = useToast()

/** Same drag-to-resize rail as the Circulars results pane. */
const libraryPane = useResizablePane('sbp:lawsRailWidth', 300, 220, 560)

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

const matchTotal = ref(0)
const expandedMatches = ref<Set<string>>(new Set())
/** Hits shown per container before the group folds; enough to judge, short enough to scan. */
const MATCH_PREVIEW = 4

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

/**
 * Topic series — authored here, because nothing in the data supports them.
 *
 * SBP lists its rulebook flat, and every candidate grouping key is empty or useless:
 * `page_slug` is null for 68 of the 75 top-level documents (only containers have one),
 * every `source_url` is the same `/assets/documents/laws_regulations/` directory, and
 * `tags`/`summary` have never been generated. So the relationship has to be stated.
 *
 * It is stated by hand rather than inferred, because inference fails on exactly the
 * cases that matter. A shared-title-prefix rule finds "Prudential Regulations for …"
 * but misses both of its FAQs — SBP named them backwards ("FAQs - Prudential
 * Regulations for SME Financing") — and finds the two "AML/CFT/CPF Regulations - …"
 * satellites while missing their head document, which is titled "Anti-Money
 * Laundering, Combating the Financing of Terrorism & …". A keyword rule does worse:
 * 15 of 75 titles match two topics and 15 match none.
 *
 * Keyed on `display_title`, not `title`, so a document does not silently fall out of
 * its series when SBP bumps a version suffix — the suffix is already split off
 * server-side. Order within a series is authored, not alphabetical: the head document
 * leads and each FAQ follows the regulation it answers for. A title that is not listed
 * here simply renders as its own row, so a document SBP adds later is never hidden.
 */
interface LawSeries {
  key: string
  label: string
  titles: string[]
}

const SERIES: LawSeries[] = [
  {
    key: 'prudential',
    label: 'Prudential Regulations',
    titles: [
      'Prudential Regulations for Corporate/Commercial Banking',
      'Prudential Regulations for Corporate/Commercial Banking FAQs',
      'Prudential Regulations for Consumer Financing',
      'Prudential Regulations for Microfinance Banks',
      'Prudential Regulations for SME Financing',
      'FAQs - Prudential Regulations for SME Financing',
      'Prudential Regulations for Housing Finance',
      'FAQs-Housing Finance Prudential Regulations',
      'Prudential Regulations for Agriculture Financing',
      'Prudential Regulations for Infrastructure Project Finance (IPF)',
    ],
  },
  {
    key: 'aml',
    label: 'AML / CFT / CPF',
    titles: [
      'Anti-Money Laundering, Combating the Financing of Terrorism & Countering Proliferation Financing (AML/CFT/CPF) Regulations',
      'AML/CFT/CPF Regulations - Guidelines on Targeted Financial Sanctions (TFS) under UNSC Resolutions',
      'AML/CFT/CPF Regulations - Frequently Asked Questions (FAQs) on Targeted Financial Sanctions (TFS) Obligations',
      'Guidelines on Risk based Approach for banks/DFIs/MFBs',
    ],
  },
  {
    key: 'credit-bureaus',
    label: 'Credit bureaus',
    titles: [
      'Credit Bureau Act 2015',
      'Credit Bureaus Amendment Act 2016',
      'Credit Bureau Rules, 2016',
      'Credit Bureau Regulations, 2016',
      'Credit Bureaus Licensing Criteria',
      'Order: Minimum paid up Capital for Credit Bureaus',
    ],
  },
  {
    key: 'sbp',
    label: 'State Bank of Pakistan',
    titles: [
      'State Bank of Pakistan Act, 1956',
      'State Bank of Pakistan (Banking Services Corporation) Ordinance, 2001',
      'Regulations for Lender of Last Resort (LOLR) Facility under Section 17G of the State Bank of Pakistan Act, 1956',
    ],
  },
  {
    key: 'foreign-exchange',
    label: 'Foreign exchange',
    titles: [
      'Foreign Exchange Regulation Act, 1947',
      'Foreign Exchange Manual',
      'Regulatory Framework for Exchange Companies',
    ],
  },
  {
    key: 'coinage',
    label: 'Currency & coinage',
    titles: ['Pakistan Coinage Act, 1906', 'Pakistan Coinage (Amendment) Act, 2013'],
  },
  {
    key: 'microfinance',
    label: 'Microfinance',
    titles: [
      'Microfinance Institutions Ordinance, 2001',
      'Licensing Requirements and Guidelines for Setting Up Microfinance Banks',
      'Guidelines for Commercial Banks to undertake Microfinance Business',
      'Guidelines for Mobile Banking Operations of Microfinance Banks/Institutions',
    ],
  },
  {
    key: 'islamic',
    label: 'Islamic banking',
    titles: [
      'Instructions & Guidelines for Shariah compliance',
      'Guidelines and Criteria for Establishing Islamic Banking Institutions (IBIs) and Commencement of Shariah Compliant Business and Operations by Development Finance Institutions (DFIs)',
      'Guidelines for Conversion of a Conventional Bank into an Islamic Bank',
      'Criteria for Conversion of Conventional Banking Branches Into Islamic Banking Branches',
      'Revised Instructions on Islamic Banking Windows (IBWs) Operations',
      'Risk Management Guidelines for Islamic Banking Institutions',
      'Guidelines for Islamic Microfinance Business',
      'Guidelines for Islamic Microfinance Business by Financial Institutions',
      'Guidelines on Islamic Financing for Agriculture',
    ],
  },
  {
    key: 'payments',
    label: 'Payments, cards & ATMs',
    titles: [
      'Payment Systems and Electronic Fund Transfer Act, 2007',
      'Electronic Transactions Ordinance, 2002',
      'PSD Guidelines for Standardisation of ATM Operations',
      'Draft White Label ATM Guidelines for feedback',
      'PSD Guidelines for Account Holders using Credit/Debit/Smart Cards (URDU)',
      'Frequently Asked Questions (FAQs) on use of Biometric Technology',
      'Digital Financial Services (DFS) - Innovation Challenge Facility',
      'Revised Guidelines- Digital Financial Services (DFS) Innovation Challenge Facility (ICF)',
    ],
  },
  {
    key: 'reporting',
    label: 'Statistical reporting',
    titles: [
      'Reporting Guidelines',
      'Reporting Guides - Monetary and Financial Statistics',
      'Foreign Exchange Returns-Coding system guide',
      'Guidelines for Foreign Investment Survey (FIS)',
      'Guidelines for Coordinated Portfolio Investment Survey (CPIS)',
      'Data Revision Policy',
    ],
  },
  {
    key: 'agriculture',
    label: 'Agriculture financing',
    titles: [
      'Guidelines on Horticulture Financing',
      'Guidelines for Poultry Financing',
      'ACD - Guidelines Fisheries',
    ],
  },
  {
    key: 'investor',
    label: "Investor's guidelines",
    titles: [
      "Investor's Guidelines for Market Treasury Bills",
      "Investor's Guidelines for Pakistan Investment Bonds",
      "Investor's Guidelines for GOP Ijara Sukuk",
    ],
  },
  {
    key: 'licensing',
    label: 'Licensing & establishment',
    titles: [
      'Branch Licensing Policy',
      'Guidelines and Criteria for Setting Up a Commercial Bank',
      'Policy for Opening of Overseas Offices & Establishment of a Subsidiary Banking Company Outside Pakistan',
    ],
  },
]

const SERIES_BY_TITLE = new Map<string, { series: LawSeries; rank: number }>()
for (const series of SERIES) {
  series.titles.forEach((title, rank) => SERIES_BY_TITLE.set(title, { series, rank }))
}

/** Expansion keys are shared between series and containers; namespace them apart. */
function seriesKey(key: string): string {
  return `series:${key}`
}

/**
 * A series row and a standalone document row are both "one named thing you can open",
 * so they sort together alphabetically rather than living in separate blocks. That is
 * the same shape containers already had — an expandable row among plain ones.
 */
type TreeEntry =
  | { kind: 'series'; key: string; label: string; items: LawSummary[] }
  | { kind: 'doc'; key: string; label: string; doc: LawSummary }

const treeEntries = computed<TreeEntry[]>(() => {
  const tops = documents.value.filter(
    (doc) => !doc.parent_id && (!typeFilter.value || doc.doc_type === typeFilter.value),
  )
  const bySeries = new Map<string, LawSummary[]>()
  const entries: TreeEntry[] = []

  for (const doc of tops) {
    const found = SERIES_BY_TITLE.get(doc.display_title)
    if (!found) {
      entries.push({ kind: 'doc', key: doc.id, label: doc.display_title, doc })
      continue
    }
    const bucket = bySeries.get(found.series.key) || []
    bucket.push(doc)
    bySeries.set(found.series.key, bucket)
  }

  for (const series of SERIES) {
    const items = bySeries.get(series.key)
    if (!items?.length) continue
    items.sort(
      (a, b) =>
        (SERIES_BY_TITLE.get(a.display_title)?.rank ?? 0) -
        (SERIES_BY_TITLE.get(b.display_title)?.rank ?? 0),
    )
    // A series of one is not a series — under a type filter most of them shrink to
    // that, and a folder holding a single row is pure friction.
    if (items.length === 1) {
      entries.push({ kind: 'doc', key: items[0].id, label: items[0].display_title, doc: items[0] })
      continue
    }
    entries.push({ kind: 'series', key: series.key, label: series.label, items })
  }

  return entries.sort((a, b) => a.label.localeCompare(b.label))
})

/**
 * Filter chips. `/api/laws/types` decides which types exist — so a type we have not
 * hardcoded still gets a chip — but the count shown is the number of *documents the
 * tree holds*, i.e. top-level only. The API's own count spans the whole corpus and
 * would contradict what the user is looking at; it goes in the tooltip instead.
 */
const facets = computed(() => {
  const topLevel = new Map<string, number>()
  for (const doc of documents.value) {
    if (doc.parent_id) continue
    const key = doc.doc_type || 'other'
    topLevel.set(key, (topLevel.get(key) || 0) + 1)
  }
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

const visibleTotal = computed(() =>
  treeEntries.value.reduce((sum, entry) => sum + (entry.kind === 'series' ? entry.items.length : 1), 0),
)

/**
 * The type mix of a series, in authority order — the signal the type headers used to
 * carry. Silent under an active filter, where every member is that type by definition.
 */
function seriesTypes(items: LawSummary[]): string {
  if (typeFilter.value) return ''
  const names = [...new Set(items.map((item) => item.doc_type || 'other'))].sort(byAuthority)
  return names.map(typeLabel).join(', ')
}

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

/**
 * The rest of the series the open document belongs to.
 *
 * This is the half of grouping the tree cannot deliver: reading the SME Financing
 * regulations, its own FAQ document is thirty rows away under "F", and nothing on
 * the page says it exists. A part resolves through its container, so a chapter of
 * the Foreign Exchange Manual offers the FE Regulation Act rather than its own
 * sibling chapters — those are already one click away in the tree.
 */
const seriesSiblings = computed(() => {
  const doc = detail.value
  if (!doc) return null
  const anchorTitle = doc.parent?.display_title || doc.display_title
  const anchorId = doc.parent?.id || doc.id
  const found = SERIES_BY_TITLE.get(anchorTitle)
  if (!found) return null
  const items = found.series.titles
    .map((title) => documents.value.find((item) => item.display_title === title))
    .filter((item): item is LawSummary => !!item && item.id !== anchorId)
  return items.length ? { label: found.series.label, items } : null
})

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

/* ---- AI analysis rail ------------------------------------------------------------ */

const analysisRail = useResizablePane('sbp:lawRailWidth', 336, 240, 480, { reverse: true })
const generationPopover = ref<InstanceType<typeof Popover> | null>(null)
const exportingChecklist = ref(false)
// Generation writes the shared corpus and is admin-only on the server; the controls stay
// visible and explain themselves rather than vanishing for a tester.
const { isAdmin, load: loadCurrentUser } = useCurrentUser()

const { activeJob, generate: startGeneration, stop: stopGeneration } = useAiGeneration({
  start: (feature) => startLawGeneration(selectedId.value, feature as LawGenerationAction),
  refresh: async () => {
    detail.value = await getLawDetail(selectedId.value)
  },
  subject: 'document',
})

/** A collection has no wording of its own, so only these can be built from its parts. */
const CONTAINER_FEATURES: LawGenerationFeature[] = ['summary', 'tags', 'relationships']

const isCollection = computed(() => (detail.value?.children.length ?? 0) > 0)

const generationFeatures = computed(() => {
  const all: Array<{ feature: LawGenerationFeature; label: string; icon: string }> = [
    { feature: 'summary', label: 'Summary', icon: 'pi pi-align-left' },
    { feature: 'tags', label: 'Tags', icon: 'pi pi-tags' },
    { feature: 'checklist', label: 'Checklist', icon: 'pi pi-list-check' },
    { feature: 'relationships', label: 'Relationships', icon: 'pi pi-share-alt' },
    { feature: 'entities', label: 'Regulatory Values', icon: 'pi pi-percentage' },
  ]
  // Offering a checklist on the FE Manual would be offering a button that always fails.
  return isCollection.value
    ? all.filter((item) => CONTAINER_FEATURES.includes(item.feature))
    : all
})

function hasGenerated(feature: LawGenerationFeature): boolean {
  return Boolean(detail.value?.generation?.[feature])
}

function generationLabel(feature: LawGenerationFeature, label: string): string {
  return `${hasGenerated(feature) ? 'Regenerate' : 'Generate'} ${label}`
}

/**
 * What "Generate All" covers on the backend. The checklist is the priciest feature — one
 * model call per chunk of a document that can run to hundreds of pages — so `all` skips it
 * and it is generated on request instead.
 */
const allGenerated = computed(() =>
  generationFeatures.value
    .filter(({ feature }) => feature !== 'checklist')
    .every(({ feature }) => hasGenerated(feature)),
)

/**
 * Why this document can never be analysed, or '' when it can.
 *
 * The backend says the same thing on a 422, but a button that explains itself only after
 * being pressed is a button that should not have been offered. 33 of the 133 documents in
 * the corpus are in this state, for six different reasons.
 */
const analysisBlocker = computed(() => {
  const doc = detail.value
  if (!doc) return ''
  if (doc.circular_id) return 'This entry is a circular — its analysis lives with the circular.'
  if (doc.is_external) return 'Published outside SBP, so we hold no copy to analyse.'
  if (isCollection.value) return ''
  if (!doc.current_version) return "SBP's link to this file is broken, so there is nothing to analyse."
  if (doc.current_version.file_type === 'xls') return 'This document is a spreadsheet, which we cannot read.'
  return ''
})

/** A collection can be summarised only once its parts have been. */
const rollupPending = computed(
  () => isCollection.value && !detail.value?.children.some((child) => child.has_content),
)

const LAW_RELATION_LABELS: Record<string, string> = {
  made_under: 'Made under',
  amends: 'Amends',
  repeals: 'Repeals',
  references: 'References',
}

/** The same edge read from the other end. "Made under" becomes "Issued under it". */
const LAW_RELATION_INCOMING: Record<string, string> = {
  made_under: 'Issued under this',
  amends: 'Amended by',
  repeals: 'Repealed by',
  references: 'Referenced by',
}

// Authority order: what this instrument sits under matters more than what it mentions.
const LAW_RELATION_ORDER = ['made_under', 'amends', 'repeals', 'references']

function buildLawGroups(
  edges: LawRelationship[],
  direction: 'outgoing' | 'incoming',
): RelationGroup[] {
  const groups = new Map<string, RelationGroup>()
  for (const edge of edges) {
    const type = edge.type || 'references'
    const key = `${direction}:${type}`
    let group = groups.get(key)
    if (!group) {
      const labels = direction === 'incoming' ? LAW_RELATION_INCOMING : LAW_RELATION_LABELS
      group = { key, direction, type, label: labels[type] ?? type, items: [] }
      groups.set(key, group)
    }
    group.items.push({
      id: edge.document?.id ?? null,
      label: edge.document?.display_title || edge.target_reference || 'Unresolved',
      crumb: edge.document?.part_label,
    })
  }
  return [...groups.values()]
}

const lawRelationshipGroups = computed<RelationGroup[]>(() => {
  const edges = detail.value?.relationships
  if (!edges) return []
  const groups = [
    ...buildLawGroups(edges.outgoing, 'outgoing'),
    ...buildLawGroups(edges.incoming, 'incoming'),
  ]
  const rank = (type?: string) => {
    const index = LAW_RELATION_ORDER.indexOf(type || '')
    return index === -1 ? LAW_RELATION_ORDER.length : index
  }
  return groups.sort((a, b) => {
    if (a.direction !== b.direction) return a.direction === 'outgoing' ? -1 : 1
    return rank(a.type) - rank(b.type)
  })
})

/**
 * The circulars acting on this law, grouped by what they do to it.
 *
 * This is the direction a reader of a regulation asks about, and until the relationships
 * pass runs they all sit under "References" — the typed groups are what tells the nine
 * circulars that amend a regulation from the two dozen that merely mention it.
 */
const citedByGroups = computed<RelationGroup[]>(() => {
  const links = detail.value?.linked_circulars ?? []
  const groups = new Map<string, RelationGroup>()
  for (const link of links) {
    const type = link.link_type || 'references'
    const key = `cited:${type}`
    let group = groups.get(key)
    if (!group) {
      const labels: Record<string, string> = {
        amends: 'Amended by',
        implements: 'Implemented by',
        clarifies: 'Clarified by',
        annexure_of: 'Annexure of',
        listing: 'Listed with',
        references: 'Referenced by',
      }
      group = { key, direction: 'incoming', type, label: labels[type] ?? type, items: [] }
      groups.set(key, group)
    }
    group.items.push({
      id: link.circular.id,
      label: link.circular.reference || link.circular.title,
      crumb: link.circular.title,
    })
  }
  const rank = (type?: string) =>
    ['amends', 'implements', 'clarifies', 'annexure_of', 'listing', 'references']
      .indexOf(type || '')
  return [...groups.values()].sort((a, b) => rank(a.type) - rank(b.type))
})

const hasAnalysis = computed(() =>
  Boolean(
    detail.value?.summary ||
      lawRelationshipGroups.value.length ||
      citedByGroups.value.length ||
      detail.value?.entities?.length ||
      detail.value?.tags?.length,
  ),
)

/**
 * Whether anything in the rail actually came from a model.
 *
 * Distinct from `hasAnalysis` because the "Circulars" section is deterministic — 57 of the
 * 133 documents carry name-matched links and would render a populated-looking rail having
 * never been analysed. Without this the invitation to analyse would never appear on
 * exactly the documents most worth analysing.
 */
const hasAiAnalysis = computed(() =>
  Boolean(
    detail.value?.summary ||
      detail.value?.tags?.length ||
      detail.value?.entities?.length ||
      lawRelationshipGroups.value.length ||
      Object.values(detail.value?.generation ?? {}).some(Boolean),
  ),
)

function generate(feature: LawGenerationAction) {
  generationPopover.value?.hide()
  void startGeneration(feature)
}

function openCircular(id: string) {
  void router.push(`/circulars/${id}`)
}

function openLaw(id: string) {
  void router.push(`/laws/${encodeURIComponent(id)}`)
}

async function exportChecklist() {
  if (!detail.value) return
  exportingChecklist.value = true
  try {
    await downloadLawChecklistExcel(detail.value.id, detail.value.display_title)
  } catch (error) {
    toast.add({
      severity: 'error',
      summary: 'Export failed',
      detail: error instanceof Error ? error.message : 'Unable to export the checklist.',
      life: 5000,
    })
  } finally {
    exportingChecklist.value = false
  }
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

/** Some parts are titled with their own label ("NBFCs"); repeating it reads as a stutter. */
function partLabelOf(doc: { part_label?: string | null; display_title: string }): string {
  const label = doc.part_label
  return !label || label === doc.display_title ? '' : label
}

interface MatchGroup {
  key: string
  /** The container's own hit, when the container itself matched. */
  self: LawSummary | null
  containerId: string
  containerTitle: string
  /** Matching parts, in relevance order. */
  hits: LawSummary[]
}

/**
 * Search hits, folded back into the hierarchy (plan §1.2).
 *
 * Flat, a search for "export proceeds" is 49 rows of which 20-odd are Foreign Exchange
 * Manual chapters sitting as siblings of unrelated documents — the container is the
 * document, so its chapters belong under it. Groups keep the server's relevance order:
 * a group takes the position of its best-ranked hit, so the most relevant thing is still
 * at the top whether it is a whole document or one chapter of one.
 */
const matchGroups = computed<MatchGroup[]>(() => {
  const list = matches.value
  if (!list) return []
  const groups = new Map<string, MatchGroup>()
  const order: string[] = []

  for (const item of list) {
    const key = item.parent_id || item.id
    let group = groups.get(key)
    if (!group) {
      group = {
        key,
        self: null,
        containerId: item.parent_id || item.id,
        containerTitle: item.parent_title || item.display_title,
        hits: [],
      }
      groups.set(key, group)
      order.push(key)
    }
    if (item.parent_id) {
      group.hits.push(item)
    } else {
      group.self = item
      // A container that matched on its own title knows its real name; a group built
      // from a part only knows what `parent_title` told it.
      group.containerTitle = item.display_title
    }
  }
  return order.map((key) => groups.get(key) as MatchGroup)
})

const searchSummary = computed(() => {
  if (searching.value) return 'Searching…'
  const shown = matches.value?.length || 0
  if (!shown) return 'No matches'
  const documents = matchGroups.value.length
  const summary =
    `${matchTotal.value} match${matchTotal.value === 1 ? '' : 'es'}` +
    ` in ${documents} document${documents === 1 ? '' : 's'}`
  // Say so rather than quietly showing a slice of the answer.
  return shown < matchTotal.value ? `${summary} · showing the top ${shown}` : summary
})

function visibleHits(group: MatchGroup): LawSummary[] {
  if (expandedMatches.value.has(group.key) || group.hits.length <= MATCH_PREVIEW) return group.hits
  return group.hits.slice(0, MATCH_PREVIEW)
}

function toggleMatchGroup(key: string) {
  const next = new Set(expandedMatches.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expandedMatches.value = next
}

/** Opening a group opens the container, whether or not the container itself matched. */
function openGroup(group: MatchGroup) {
  const doc = group.self || documents.value.find((item) => item.id === group.containerId)
  if (doc) select(doc)
}

/**
 * The one muted line under a row: what kind of document it is, what we hold, and
 * which edition of it. The type leads because the tree no longer groups by type —
 * without it a row gives no clue whether it is an Act or a guidance note.
 */
function subLine(doc: LawSummary): string {
  // Under an active type filter every row would repeat the filter back at you.
  const type = doc.doc_type === typeFilter.value ? '' : typeLabel(doc.doc_type)
  const held = holdingsById.value.get(doc.id)
  if (held) {
    const count = `${held.parts} part${held.parts === 1 ? '' : 's'}`
    const holding = !held.held ? 'none held' : held.held === held.parts ? 'all held' : `${held.held} held`
    return [type, count, holding].filter(Boolean).join(' · ')
  }
  const edition = doc.current_version?.version_label || doc.version_suffix || ''
  const state = doc.is_external
    ? 'hosted externally'
    : doc.circular_id
      ? 'is a circular'
      : !doc.current_version
        ? 'no file held'
        : ''
  // Only when there is nothing else to say — with a type in front of it, "in force"
  // on every row is noise rather than information.
  return [type, state, edition].filter(Boolean).join(' · ') || 'in force'
}

function toggleKey(key: string) {
  const next = new Set(expanded.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expanded.value = next
}

function toggle(doc: LawSummary) {
  toggleKey(doc.id)
}

function select(doc: LawSummary) {
  if (isContainer(doc)) toggle(doc)
  if (doc.id === selectedId.value) return
  void router.push(`/laws/${encodeURIComponent(doc.id)}`)
}

/**
 * Scroll the selected row into the rail. Opening `/laws/{id}` directly used to leave
 * the rail at the top with the selection several screens below the fold — the tree
 * expanded correctly, you just could not see it. `nearest` makes this a no-op when
 * the row is already visible, so clicking around the tree does not jump the view.
 */
async function revealSelected() {
  await nextTick()
  const row = document.querySelector('.library-tree .node.is-selected')
  row?.scrollIntoView({ block: 'nearest' })
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
    // A deep link resolves its detail before the tree has any rows to scroll to,
    // so the reveal has to be re-tried once the corpus is actually rendered.
    if (selectedId.value) void revealSelected()
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
  // A job polling for the document we just navigated away from must not write its result
  // onto the one we just opened.
  stopGeneration()
  try {
    detail.value = await getLawDetail(id)
    // Keep a part's container open so the reader is never shown without its context,
    // and open a container reached by deep link so its parts are there to pick from.
    const next = new Set(expanded.value)
    const toOpen = detail.value.parent?.id || (detail.value.children.length ? detail.value.id : '')
    if (toOpen) next.add(toOpen)
    // …and the series it sits in, or a deep link lands on a row inside a shut folder.
    const anchorTitle = detail.value.parent?.display_title || detail.value.display_title
    const found = SERIES_BY_TITLE.get(anchorTitle)
    if (found) next.add(seriesKey(found.series.key))
    expanded.value = next
    void revealSelected()
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
    matchTotal.value = 0
    searching.value = false
    return
  }

  searching.value = true
  expandedMatches.value = new Set()
  searchController = new AbortController()
  // 100 is the API's ceiling. Worth asking for now that hits fold into their container —
  // "bank" matches all 133 documents, and 50 flat rows was a silent truncation.
  getLaws({ q: value, doc_type: typeFilter.value || undefined, per_page: 100 }, searchController.signal)
    .then((response) => {
      matches.value = response.items
      matchTotal.value = response.total
    })
    .catch((error) => {
      if ((error as Error).name !== 'AbortError') {
        matches.value = []
        matchTotal.value = 0
      }
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
  void loadCurrentUser()
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
  <div
    class="laws-view sbp-pane-view is-resizable"
    :style="{ '--sbp-rail-width': `${libraryPane.size.value}px` }"
  >
    <aside class="laws-library sbp-rail">
      <header class="sbp-rail-head">
        <span class="sbp-rail-title">Laws &amp; Regulations</span>
        <span class="sbp-rail-count">{{ visibleTotal || '—' }}</span>
      </header>

      <div class="sbp-search-field">
        <i class="pi pi-search" />
        <input v-model="query" type="search" placeholder="Filter or search full text" />
        <button
          v-if="query"
          type="button"
          class="sbp-search-clear"
          aria-label="Clear search"
          @click="query = ''"
        ><i class="pi pi-times" /></button>
      </div>

      <div v-if="facets.length > 1" class="library-facets">
        <button
          type="button"
          class="sbp-filter-pill"
          :class="{ 'is-active': !typeFilter }"
          @click="typeFilter = ''"
        >All <span class="sbp-filter-pill-count">{{ topLevelTotal }}</span></button>
        <button
          v-for="facet in facets"
          :key="facet.doc_type"
          type="button"
          class="sbp-filter-pill"
          :class="{ 'is-active': typeFilter === facet.doc_type }"
          :title="`${facet.corpusCount} in the corpus, including parts`"
          :aria-label="`${typeLabel(facet.doc_type)}, ${facet.count} documents`"
          @click="typeFilter = typeFilter === facet.doc_type ? '' : facet.doc_type"
        >{{ typeLabel(facet.doc_type) }} <span class="sbp-filter-pill-count">{{ facet.count }}</span></button>
      </div>

      <p v-if="listLoading" class="library-note">Loading the corpus…</p>
      <p v-else-if="listError" class="library-note is-error">{{ listError }}</p>

      <!-- Search results, folded back into the hierarchy: chapters sit under their manual. -->
      <div v-else-if="matches" class="library-tree">
        <p class="library-note">{{ searchSummary }}</p>

        <template v-for="group in matchGroups" :key="group.key">
          <!-- A document whose parts did not match — nothing to nest. -->
          <button
            v-if="group.self && !group.hits.length"
            type="button"
            class="sbp-row node is-flat"
            :class="{ 'is-selected': group.self.id === selectedId }"
            @click="select(group.self)"
          >
            <span class="node-body">
              <span class="sbp-row-title">{{ group.self.display_title }}</span>
            </span>
          </button>

          <template v-else>
            <button
              type="button"
              class="sbp-row node is-flat"
              :class="{ 'is-selected': group.containerId === selectedId }"
              @click="openGroup(group)"
            >
              <span class="node-body">
                <span class="sbp-row-title">{{ group.containerTitle }}</span>
                <span class="sbp-row-sub">
                  {{ group.hits.length }} matching part{{ group.hits.length === 1 ? '' : 's' }}
                  <template v-if="group.self"> · and the document itself</template>
                </span>
              </span>
            </button>

            <div class="node-children">
              <button
                v-for="hit in visibleHits(group)"
                :key="hit.id"
                type="button"
                class="sbp-row node is-child"
                :class="{ 'is-selected': hit.id === selectedId }"
                @click="select(hit)"
              >
                <span class="node-body">
                  <span class="sbp-row-title">
                    <span v-if="partLabelOf(hit)" class="node-part">{{ partLabelOf(hit) }}</span>
                    {{ hit.display_title }}
                  </span>
                </span>
              </button>
              <button
                v-if="group.hits.length > MATCH_PREVIEW"
                type="button"
                class="match-more"
                @click="toggleMatchGroup(group.key)"
              >
                {{ expandedMatches.has(group.key)
                  ? 'Show fewer'
                  : `+${group.hits.length - MATCH_PREVIEW} more` }}
              </button>
            </div>
          </template>
        </template>
      </div>

      <!--
        Series folders and standalone documents, interleaved alphabetically. SBP lists
        the corpus flat; the series map in the script is ours, so "Prudential
        Regulations" reads as one entry instead of ten rows scattered under P and F.
      -->
      <div v-else class="library-tree">
        <template v-for="entry in treeEntries" :key="entry.key">
          <template v-if="entry.kind === 'series'">
            <button
              type="button"
              class="sbp-row node is-series"
              :aria-expanded="expanded.has(seriesKey(entry.key))"
              @click="toggleKey(seriesKey(entry.key))"
            >
              <span class="node-caret">
                <i
                  class="pi"
                  :class="expanded.has(seriesKey(entry.key)) ? 'pi-chevron-down' : 'pi-chevron-right'"
                />
              </span>
              <span class="node-body">
                <span class="sbp-row-title">{{ entry.label }}</span>
                <span class="sbp-row-sub">
                  {{ [`${entry.items.length} documents`, seriesTypes(entry.items)].filter(Boolean).join(' · ') }}
                </span>
              </span>
            </button>

            <div v-if="expanded.has(seriesKey(entry.key))" class="node-children">
              <template v-for="doc in entry.items" :key="doc.id">
                <button
                  type="button"
                  class="sbp-row node"
                  :class="{ 'is-selected': doc.id === selectedId }"
                  :aria-expanded="isContainer(doc) ? expanded.has(doc.id) : undefined"
                  @click="select(doc)"
                >
                  <span class="node-caret">
                    <i
                      v-if="isContainer(doc)"
                      class="pi"
                      :class="expanded.has(doc.id) ? 'pi-chevron-down' : 'pi-chevron-right'"
                    />
                  </span>
                  <span class="node-body">
                    <span class="sbp-row-title">{{ doc.display_title }}</span>
                    <span class="sbp-row-sub">{{ subLine(doc) }}</span>
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
                    class="sbp-row node is-child"
                    :class="{ 'is-selected': child.id === selectedId }"
                    @click="select(child)"
                  >
                    <span class="node-body">
                      <span class="sbp-row-title">
                        <span v-if="partLabelOf(child)" class="node-part">{{ partLabelOf(child) }}</span>
                        {{ child.display_title }}
                      </span>
                      <span v-if="!child.current_version?.has_file" class="sbp-row-sub">not held</span>
                    </span>
                  </button>
                </div>
              </template>
            </div>
          </template>

          <template v-else>
            <button
              type="button"
              class="sbp-row node"
              :class="{ 'is-selected': entry.doc.id === selectedId }"
              :aria-expanded="isContainer(entry.doc) ? expanded.has(entry.doc.id) : undefined"
              @click="select(entry.doc)"
            >
              <span class="node-caret">
                <i
                  v-if="isContainer(entry.doc)"
                  class="pi"
                  :class="expanded.has(entry.doc.id) ? 'pi-chevron-down' : 'pi-chevron-right'"
                />
              </span>
              <span class="node-body">
                <span class="sbp-row-title">{{ entry.doc.display_title }}</span>
                <span class="sbp-row-sub">{{ subLine(entry.doc) }}</span>
                <!-- Only where the collection is incomplete: a full bar says nothing. -->
                <span
                  v-if="holdingsById.get(entry.doc.id) && holdingsById.get(entry.doc.id)!.held < holdingsById.get(entry.doc.id)!.parts"
                  class="node-meter"
                  aria-hidden="true"
                >
                  <span
                    class="node-meter-fill"
                    :style="{ width: `${(holdingsById.get(entry.doc.id)!.held / holdingsById.get(entry.doc.id)!.parts) * 100}%` }"
                  />
                </span>
              </span>
            </button>

            <div v-if="isContainer(entry.doc) && expanded.has(entry.doc.id)" class="node-children">
              <button
                v-for="child in childrenByParent.get(entry.doc.id)"
                :key="child.id"
                type="button"
                class="sbp-row node is-child"
                :class="{ 'is-selected': child.id === selectedId }"
                @click="select(child)"
              >
                <span class="node-body">
                  <span class="sbp-row-title">
                    <span v-if="partLabelOf(child)" class="node-part">{{ partLabelOf(child) }}</span>
                    {{ child.display_title }}
                  </span>
                  <!-- The container's meter says how many are missing; this says which. -->
                  <span v-if="!child.current_version?.has_file" class="sbp-row-sub">not held</span>
                </span>
              </button>
            </div>
          </template>
        </template>
      </div>
    </aside>

    <div
      class="pane-resizer"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize the library panel"
      :class="{ resizing: libraryPane.resizing.value }"
      @pointerdown="libraryPane.startDrag"
      @dblclick="libraryPane.resetToDefault"
    />

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
        <header class="reader-head sbp-pane-head">
          <div class="reader-identity">
            <p v-if="detail.parent" class="reader-crumb">{{ detail.parent.display_title }}</p>
            <h1 class="reader-title sbp-pane-title">
              <span v-if="partLabelOf(detail)" class="reader-part">{{ partLabelOf(detail) }}</span>
              {{ detail.display_title }}
            </h1>
            <p class="reader-status sbp-pane-substatus">
              <span class="sbp-badge">{{ typeLabel(detail.doc_type) }}</span>
              <span v-if="currentVersion" class="reader-force">In force</span>
              <span v-if="statusLine">{{ statusLine }}</span>
              <span v-for="tag in detail.tags" :key="tag" class="intelligence-pill tag-pill">{{ tag }}</span>
            </p>
          </div>
          <div class="reader-actions">
            <span
              v-if="!analysisBlocker"
              class="admin-gate"
              :title="isAdmin
                ? (activeJob ? `Generating ${activeJob.feature}` : 'Generate AI analysis')
                : adminOnlyHint('AI analysis')"
            >
              <Button
                icon="pi pi-sparkles"
                text
                rounded
                severity="help"
                :loading="Boolean(activeJob)"
                :disabled="!isAdmin"
                :aria-label="isAdmin ? 'Generate AI analysis' : adminOnlyLabel('Generate AI analysis')"
                @click="generationPopover?.toggle($event)"
              />
            </span>
            <Button
              v-if="detail.checklist_available"
              icon="pi pi-file-excel"
              text
              rounded
              severity="success"
              :loading="exportingChecklist"
              aria-label="Export checklist to Excel"
              title="Export the obligations checklist"
              @click="exportChecklist"
            />
            <a
              v-if="fileUrl"
              class="sbp-ghost-button"
              :href="fileUrl"
              target="_blank"
              rel="noreferrer"
            >Open in a new tab</a>
          </div>
        </header>

        <Popover ref="generationPopover" class="generation-popover">
          <div class="generation-menu">
            <span class="generation-menu-title">AI analysis</span>
            <p v-if="isCollection" class="generation-menu-note">
              A collection is summarised from its parts.
            </p>
            <Button
              v-for="item in generationFeatures"
              :key="item.feature"
              :icon="item.icon"
              :label="generationLabel(item.feature, item.label)"
              text
              size="small"
              :disabled="Boolean(activeJob)"
              @click="generate(item.feature)"
            />
            <div class="generation-menu-divider" />
            <Button
              icon="pi pi-sparkles"
              :label="allGenerated ? 'Regenerate All' : 'Generate All'"
              size="small"
              :disabled="Boolean(activeJob)"
              @click="generate('all')"
            />
          </div>
        </Popover>

        <div v-if="activeJob" class="generation-progress" role="status">
          <i class="pi pi-sparkles" />
          Generating {{ activeJob.feature === 'all' ? 'all AI analysis' : activeJob.feature }} in the background
          <span v-if="activeJob.progress_total">
            ({{ activeJob.progress_completed }}/{{ activeJob.progress_total }} steps)
          </span>
        </div>

        <div class="reader-body" :style="{ '--detail-rail-width': `${analysisRail.size.value}px` }">

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
            <RouterLink class="sbp-ghost-button" :to="`/circulars/${detail.circular_id}`">Open the circular</RouterLink>
          </template>
          <template v-else-if="detail.is_external">
            <h2>Hosted outside SBP.</h2>
            <p>This one is published elsewhere, so we hold no copy of it — but the circulars that cite it are here.</p>
            <a v-if="detail.source_url" class="sbp-ghost-button" :href="detail.source_url" target="_blank" rel="noreferrer">
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

        <div
          class="pane-resizer detail-resizer"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize the analysis panel"
          :class="{ resizing: analysisRail.resizing.value }"
          @pointerdown="analysisRail.startDrag"
          @dblclick="analysisRail.resetToDefault"
        />

        <aside class="detail-rail" aria-label="Document analysis">
          <template v-if="hasAnalysis">
            <SummarySection v-if="detail.summary" :summary="detail.summary" />

            <section v-if="lawRelationshipGroups.length" class="detail-section intelligence-section">
              <div class="pill-group">
                <h2><i class="pi pi-sitemap section-icon" />Related instruments</h2>
                <RelationshipGroups :groups="lawRelationshipGroups" @select="openLaw" />
              </div>
            </section>

            <!-- The mirror of a circular's "Regulations cited", and the direction a
                 reader of a regulation actually asks about. -->
            <section v-if="citedByGroups.length" class="detail-section intelligence-section">
              <div class="pill-group">
                <h2><i class="pi pi-file section-icon" />Circulars</h2>
                <RelationshipGroups :groups="citedByGroups" @select="openCircular" />
              </div>
            </section>

            <RegulatoryValueList :entities="detail.entities ?? []" />
          </template>

          <!--
            Shown whenever nothing in the rail came from a model — including when the
            deterministic "Circulars" section above has filled it out, which is the common
            case and the one where the invitation matters most.
          -->
          <template v-if="!hasAiAnalysis">
            <!-- Structural: no re-run changes it, so no button is offered. -->
            <div v-if="analysisBlocker" class="detail-rail-empty" :class="{ 'is-footer': hasAnalysis }">
              <i class="pi pi-ban" />
              <p class="detail-rail-empty-title">Not analysable</p>
              <p class="detail-rail-empty-text">{{ analysisBlocker }}</p>
            </div>

            <!-- Recoverable: analyse the parts and this becomes possible. -->
            <div v-else-if="rollupPending" class="detail-rail-empty" :class="{ 'is-footer': hasAnalysis }">
              <i class="pi pi-sitemap" />
              <p class="detail-rail-empty-title">Analyse the parts first</p>
              <p class="detail-rail-empty-text">
                A collection is summarised from its parts, and none of this one's parts are held.
              </p>
            </div>

            <div v-else class="detail-rail-empty" :class="{ 'is-footer': hasAnalysis }">
              <i class="pi pi-sparkles" />
              <p class="detail-rail-empty-title">No AI analysis yet</p>
              <p class="detail-rail-empty-text">
                A summary, tags, relationships and the regulatory values this document
                states can be generated. The obligations checklist is generated separately.
                <template v-if="!isAdmin">{{ ADMIN_ONLY_EMPTY_HINT }}</template>
              </p>
              <span class="admin-gate" :title="isAdmin ? '' : adminOnlyHint('AI analysis')">
                <Button
                  icon="pi pi-sparkles"
                  label="Generate analysis"
                  size="small"
                  :loading="Boolean(activeJob)"
                  :disabled="!isAdmin"
                  @click="generate('all')"
                />
              </span>
            </div>
          </template>
        </aside>
        </div>

        <footer class="reader-provenance">
          <button type="button" class="provenance-bar" @click="provenanceOpen = !provenanceOpen">
            <span v-if="currentVersion">Held since {{ formatDate(currentVersion.first_seen_at) }}</span>
            <span v-else>Nothing archived</span>
            <span class="provenance-spacer" />
            <!-- Named, not just counted: "3 more in AML / CFT / CPF" is the reason to open this. -->
            <span v-if="seriesSiblings">
              {{ seriesSiblings.items.length }} more in {{ seriesSiblings.label }}
            </span>
            <span>Cited by {{ detail.linked_circulars.length }} circular{{ detail.linked_circulars.length === 1 ? '' : 's' }}</span>
            <i class="pi" :class="provenanceOpen ? 'pi-chevron-down' : 'pi-chevron-up'" />
          </button>

          <div v-if="provenanceOpen" class="provenance-open">
            <div>
              <h3>Custody</h3>
              <p>{{ custodyLine || 'No file has been archived for this document.' }}</p>
            </div>
            <div v-if="seriesSiblings">
              <h3>{{ seriesSiblings.label }}</h3>
              <ul>
                <li v-for="sibling in seriesSiblings.items" :key="sibling.id">
                  <RouterLink :to="`/laws/${encodeURIComponent(sibling.id)}`">
                    {{ sibling.display_title }}
                  </RouterLink>
                </li>
              </ul>
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
          {{ topLevelTotal }} documents captured from sbp.org.pk, plus
          {{ documents.length - topLevelTotal }} chapters and appendices inside them. SBP replaces
          these files in place and keeps no history; from the day we started watching, we do.
          Pick anything on the left to open it.
        </p>
      </div>
    </section>
  </div>
</template>
<style scoped>
/* The shell, rail, search field, filter pills, rows, badge, ghost button and
   pane header all come from the .sbp-* primitives in styles.css. What is left
   here is only what is genuinely specific to the laws corpus. */

/* ---- Library ---- */
.laws-library {
  gap: 0.6rem;
}

.library-facets {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
}

.library-note {
  margin: 0.3rem 0.35rem;
  font-size: var(--sbp-fs-meta);
  color: var(--sbp-muted);
}

.library-note.is-error {
  color: var(--sbp-danger);
}

.library-tree {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

/* A tree row is a .sbp-row with a caret gutter. */
.node {
  grid-template-columns: 0.85rem 1fr;
  gap: 0.4rem;
  align-items: start;
}

/* A series is a folder, not a document — it has no reader to open, so it must not
   look selectable. Weight separates it from the rows nested under it. */
.node.is-series .sbp-row-title {
  font-weight: 600;
}

.node.is-series {
  margin-top: 0.15rem;
}

/* Rows with no caret slot: without this they land in the 0.85rem caret column. */
.node.is-child,
.node.is-flat {
  grid-template-columns: 1fr;
}

.node-caret {
  padding-top: 0.15rem;
  color: var(--sbp-muted);
  font-size: var(--sbp-fs-eyebrow);
}

.node-body {
  min-width: 0;
}

.node-part {
  color: var(--sbp-muted);
  font-variant-numeric: tabular-nums;
}

.node-crumb {
  display: block;
  margin-bottom: 0.1rem;
  font-size: var(--sbp-fs-meta);
  color: var(--sbp-green-text);
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

.match-more {
  align-self: flex-start;
  margin-top: 0.1rem;
  padding: 0.2rem 0.4rem;
  border: 0;
  border-radius: var(--sbp-radius-sm);
  background: transparent;
  color: var(--sbp-muted);
  font: inherit;
  font-size: var(--sbp-fs-meta);
  cursor: pointer;
}

.match-more:hover {
  background: var(--sbp-subtle);
  color: var(--sbp-green-text);
}

/* ---- Reader ---- */
.laws-reader {
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--sbp-surface);
  overflow: hidden;
}

.reader-crumb {
  margin: 0 0 0.2rem;
  font-size: var(--sbp-fs-meta);
  color: var(--sbp-green-text);
  font-weight: 600;
}

.reader-part {
  color: var(--sbp-muted);
  font-weight: 500;
}

.reader-force {
  color: var(--sbp-green-text);
  font-weight: 600;
}

/* Source left, analysis right — the same split `CircularDetailPane` uses, so the two
   readers behave identically under a drag. */
.reader-body {
  flex: 1 1 auto;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 6px minmax(15rem, var(--detail-rail-width, 21rem));
}

.reader-actions {
  display: flex;
  align-items: center;
  gap: 0.15rem;
}

.reader-frame {
  /* An iframe is a replaced element, and grid's default `align-items: normal` stretches
     normal elements but not replaced ones — so without this it collapses to the 150px
     intrinsic height and the PDF gets a viewport a few lines tall. In the flex column
     this replaced, `flex: 1` did the same job. */
  align-self: stretch;
  width: 100%;
  height: 100%;
  min-height: 0;
  border: 0;
  background: var(--sbp-subtle);
}

/* As a footer under real sections it is a prompt, not an empty state: no vertical
   centring, a rule to separate it, and muted enough not to compete with the content. */
.detail-rail-empty.is-footer {
  flex: 0 0 auto;
  justify-content: flex-start;
  border-top: 1px solid var(--sbp-border);
  padding-top: 1rem;
}

.detail-rail-empty.is-footer i {
  font-size: 1.1rem;
  opacity: 0.5;
}

.generation-menu-note {
  margin: 0 0 0.25rem;
  max-width: 15rem;
  font-size: var(--sbp-fs-meta);
  line-height: 1.4;
  color: var(--sbp-muted);
}

/* The rail is a column of its own; the reader-empty block must not stretch to fill it. */
.reader-body > .reader-empty {
  align-self: start;
}

@media (max-width: 900px) {
  .reader-body {
    grid-template-columns: minmax(0, 1fr);
  }

  .reader-body .detail-resizer,
  .reader-body .detail-rail {
    display: none;
  }
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
  font-size: var(--sbp-fs-title);
  font-weight: 600;
}

.reader-empty p,
.reader-overview p {
  margin: 0;
  color: var(--sbp-muted);
  font-size: var(--sbp-fs-body);
  line-height: 1.6;
}

.reader-empty .sbp-ghost-button {
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
  font-size: var(--sbp-fs-meta);
  text-align: left;
  cursor: pointer;
}

.provenance-bar:hover {
  color: var(--sbp-text);
}

.provenance-spacer {
  flex: 1;
}

/* Two sections normally, three when the document sits in a series — auto-fit rather
   than a fixed 1fr 1fr so the third does not squeeze the other two into columns. */
.provenance-open {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr));
  gap: 1.6rem;
  padding: 0 1.2rem 1rem;
}

.provenance-open h3 {
  margin: 0 0 0.4rem;
}

.provenance-open p {
  margin: 0;
  font-size: var(--sbp-fs-sm);
  line-height: 1.6;
  color: var(--sbp-text);
}

.provenance-open ul {
  margin: 0;
  padding-left: 1rem;
  font-size: var(--sbp-fs-sm);
  line-height: 1.7;
}

.provenance-open a {
  color: var(--sbp-green-text);
}
</style>
