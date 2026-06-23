<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import Accordion from 'primevue/accordion'
import AccordionPanel from 'primevue/accordionpanel'
import AccordionHeader from 'primevue/accordionheader'
import AccordionContent from 'primevue/accordioncontent'
import Button from 'primevue/button'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import Tag from 'primevue/tag'
import StateBlock from '@/components/StateBlock.vue'
import {
  getEcoDataEntries,
  getEcoDataPdfSummary,
  type EcoDataEntry
} from '@/lib/api'

// Asynchronous component for PDF preview dialog
const PdfPreviewDialog = defineAsyncComponent(() => import('@/components/PdfPreviewDialog.vue'))

// Configure marked options
marked.use({
  breaks: true,
  gfm: true,
})

const allEntries = ref<EcoDataEntry[]>([])
const loading = ref(false)
const error = ref('')
const searchQuery = ref('')

// Active accordion section indexes
const activeSectionIndexes = ref<string[]>([])
const activeSubsectionIndexes = ref<Record<string, string[]>>({})

// Dialog visibility states
const pdfDialogVisible = ref(false)
const pdfTitle = ref('')
const pdfUrl = ref('')

const summaryDialogVisible = ref(false)
const summaryTitle = ref('')
const summaryUrl = ref('')
const summaryText = ref('')
const summaryLoading = ref(false)
const summaryError = ref('')

async function fetchEntries() {
  loading.value = true
  error.value = ''
  try {
    allEntries.value = await getEcoDataEntries()
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to load EcoData entries'
  } finally {
    loading.value = false
  }
}

const filteredEntries = computed(() => {
  const query = searchQuery.value.trim().toLowerCase()
  if (!query) {
    return allEntries.value
  }
  return allEntries.value.filter((e) => {
    return (
      e.description?.toLowerCase().includes(query) ||
      e.section?.toLowerCase().includes(query) ||
      e.subsection?.toLowerCase().includes(query) ||
      e.frequency?.toLowerCase().includes(query)
    )
  })
})

const quickLinks = computed(() => {
  return filteredEntries.value.filter((e) => e.is_quick_link)
})

interface GroupedSection {
  name: string
  entries: EcoDataEntry[]
  subsections: Record<string, EcoDataEntry[]>
  totalCount: number
}

const groupedSections = computed(() => {
  const sectionsMap: Record<string, GroupedSection> = {}

  for (const entry of filteredEntries.value) {
    if (entry.is_quick_link) continue

    const sec = entry.section
    const subsec = entry.subsection || ''

    if (!sectionsMap[sec]) {
      sectionsMap[sec] = {
        name: sec,
        entries: [],
        subsections: {},
        totalCount: 0,
      }
    }

    if (subsec) {
      if (!sectionsMap[sec].subsections[subsec]) {
        sectionsMap[sec].subsections[subsec] = []
      }
      sectionsMap[sec].subsections[subsec].push(entry)
    } else {
      sectionsMap[sec].entries.push(entry)
    }
    sectionsMap[sec].totalCount++
  }

  // Preserve the database sort order of sections
  const orderOfSections: string[] = []
  for (const entry of allEntries.value) {
    if (entry.is_quick_link) continue
    if (!orderOfSections.includes(entry.section)) {
      orderOfSections.push(entry.section)
    }
  }

  const result: GroupedSection[] = []
  for (const secName of orderOfSections) {
    if (sectionsMap[secName]) {
      // Order the subsections inside each section based on their first appearance in database order
      const orderOfSubsections: string[] = []
      for (const entry of allEntries.value) {
        if (entry.is_quick_link) continue
        if (entry.section === secName && entry.subsection) {
          if (!orderOfSubsections.includes(entry.subsection)) {
            orderOfSubsections.push(entry.subsection)
          }
        }
      }

      const orderedSubsections: Record<string, EcoDataEntry[]> = {}
      for (const subName of orderOfSubsections) {
        if (sectionsMap[secName].subsections[subName]) {
          orderedSubsections[subName] = sectionsMap[secName].subsections[subName]
        }
      }
      sectionsMap[secName].subsections = orderedSubsections

      result.push(sectionsMap[secName])
    }
  }
  return result
})

watch(groupedSections, (newVal) => {
  if (activeSectionIndexes.value.length === 0 && newVal.length > 0) {
    activeSectionIndexes.value = newVal.map((_, i) => String(i))
  }

  for (const section of newVal) {
    if (!(section.name in activeSubsectionIndexes.value)) {
      activeSubsectionIndexes.value[section.name] = Object.keys(section.subsections)
    }
  }
}, { immediate: true })

function frequencySeverity(freq: string): 'success' | 'info' | 'warn' | 'danger' | 'secondary' {
  const f = freq.toLowerCase()
  if (f === 'daily') return 'success'
  if (f === 'weekly') return 'info'
  if (f === 'monthly') return 'secondary'
  if (f === 'quarterly' || f === 'half yearly') return 'warn'
  if (f === 'annual') return 'danger'
  return 'secondary'
}

function openPdfPreview(entry: EcoDataEntry) {
  if (!entry.url) return
  pdfTitle.value = entry.description
  pdfUrl.value = entry.url
  pdfDialogVisible.value = true
}

async function openSummary(entry: EcoDataEntry) {
  if (!entry.url) return

  summaryTitle.value = entry.description
  summaryUrl.value = entry.url
  summaryText.value = ''
  summaryError.value = ''
  summaryDialogVisible.value = true
  summaryLoading.value = true

  try {
    const res = await getEcoDataPdfSummary(entry.url)
    if (res.error) {
      summaryError.value = res.error
    } else if (res.summary) {
      summaryText.value = res.summary
    } else {
      summaryError.value = 'No summary content returned from server.'
    }
  } catch (err) {
    summaryError.value = err instanceof Error ? err.message : 'An error occurred while generating summary.'
  } finally {
    summaryLoading.value = false
  }
}

function renderMarkdown(content: string): string {
  return DOMPurify.sanitize(marked.parse(content) as string, {
    USE_PROFILES: { html: true },
  })
}

onMounted(() => {
  void fetchEntries()
})
</script>

<template>
  <section class="view-stack eco-data-view">
    <div class="eco-data-toolbar glass-panel" style="border-radius: 1rem; padding: 1rem; margin-bottom: 1rem;">
      <span class="eco-data-search">
        <i class="pi pi-search" />
        <InputText
          v-model="searchQuery"
          placeholder="Filter by description, section, or frequency"
        />
        <Button
          v-if="searchQuery"
          icon="pi pi-times"
          severity="secondary"
          text
          rounded
          aria-label="Clear filter"
          title="Clear filter"
          @click="searchQuery = ''"
        />
      </span>

      <div v-if="quickLinks.length" class="toolbar-quick-links">
        <a
          v-for="entry in quickLinks"
          :key="entry.id"
          :href="entry.url || undefined"
          target="_blank"
          rel="noopener noreferrer"
          class="toolbar-quick-link"
          :class="{ 'is-disabled': !entry.url }"
        >
          <span>{{ entry.description }}</span>
          <i v-if="entry.url" class="pi pi-external-link" />
        </a>
      </div>

      <Button class="refresh-button" label="Refresh" icon="pi pi-refresh" :loading="loading" @click="fetchEntries" />
    </div>

    <!-- Loading State -->
    <div v-if="loading && !allEntries.length">
      <StateBlock
        state="loading"
        title="Loading Economic Data"
        message="Fetching and parsing SBP statistical indexes..."
      />
    </div>

    <!-- Error State -->
    <div v-else-if="error">
      <StateBlock
        state="error"
        title="Connection Error"
        :message="error"
        action-label="Retry"
        @action="fetchEntries"
      />
    </div>

    <!-- Empty State -->
    <div v-else-if="!allEntries.length">
      <StateBlock
        state="empty"
        title="No Economic Data Available"
        message="The statistical index cache is empty."
        action-label="Reload"
        @action="fetchEntries"
      />
    </div>

    <!-- Filtered Empty State -->
    <div v-else-if="!filteredEntries.length">
      <StateBlock
        state="empty"
        title="No Matching Entries"
        message="Try adjusting your filter text to find what you are looking for."
        action-label="Clear Filter"
        @action="searchQuery = ''"
      />
    </div>

    <!-- Main Content -->
    <div v-else class="eco-data-content">
      <!-- Sections Accordion -->
      <div class="accordion-container">
        <Accordion v-model:value="activeSectionIndexes" multiple class="section-accordion">
          <AccordionPanel
            v-for="(section, secIndex) in groupedSections"
            :key="section.name"
            :value="String(secIndex)"
          >
            <AccordionHeader>
              <div class="flex items-center justify-between w-full pr-4">
                <span class="font-semibold text-base md:text-lg text-color">{{ section.name }}</span>
                <Tag :value="`${section.totalCount} items`" severity="secondary" class="ml-auto text-xs" />
              </div>
            </AccordionHeader>
            <AccordionContent>
              <!-- Direct Entries Table -->
              <div v-if="section.entries.length" class="table-shell mb-4">
                <DataTable :value="section.entries" striped-rows table-style="min-width: 50rem">
                  <Column field="description" header="Description">
                    <template #body="{ data }">
                      <a
                        v-if="data.url"
                        :href="data.url"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="record-title"
                      >
                        {{ data.description }}
                      </a>
                      <span v-else>{{ data.description }}</span>
                    </template>
                  </Column>

                  <Column field="frequency" header="Frequency" style="width: 10rem" class="text-center">
                    <template #body="{ data }">
                      <Tag
                        v-if="data.frequency"
                        :value="data.frequency"
                        :severity="frequencySeverity(data.frequency)"
                      />
                      <span v-else class="muted-text">-</span>
                    </template>
                  </Column>

                  <Column field="format_type" header="Format" style="width: 6rem" class="text-center">
                    <template #body="{ data }">
                      <Button
                        v-if="data.format_type === 'xlsx' || data.format_type === 'xls'"
                        as="a"
                        :href="data.format_url"
                        target="_blank"
                        rel="noopener noreferrer"
                        icon="pi pi-file-excel"
                        text
                        rounded
                        severity="success"
                        title="Download Excel"
                      />
                      <Button
                        v-else-if="data.format_type === 'pdf'"
                        as="a"
                        :href="data.format_url"
                        target="_blank"
                        rel="noopener noreferrer"
                        icon="pi pi-file-pdf"
                        text
                        rounded
                        severity="danger"
                        title="Download PDF"
                      />
                      <span v-else class="muted-text">-</span>
                    </template>
                  </Column>

                  <Column field="last_update" header="Last Update" style="width: 10rem" class="text-center">
                    <template #body="{ data }">
                      <a
                        v-if="data.last_update?.toLowerCase().includes('easydata')"
                        href="https://easydata.sbp.org.pk"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="easydata-link"
                      >
                        <i class="pi pi-database" /> EasyData
                      </a>
                      <span v-else-if="data.last_update">{{ data.last_update }}</span>
                      <span v-else class="muted-text">-</span>
                    </template>
                  </Column>

                  <Column field="archive_url" header="Archive" style="width: 8rem" class="text-center">
                    <template #body="{ data }">
                      <a
                        v-if="data.archive_url"
                        :href="data.archive_url"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="archive-link"
                      >
                        <i class="pi pi-box" /> Archive
                      </a>
                      <span v-else class="muted-text">-</span>
                    </template>
                  </Column>

                  <Column header="Actions" style="width: 10rem" class="text-center">
                    <template #body="{ data }">
                      <div class="row-actions justify-center">
                        <Button
                          v-if="data.can_summarize"
                          icon="pi pi-sparkles"
                          label="Summary"
                          size="small"
                          outlined
                          severity="primary"
                          @click="openSummary(data)"
                        />
                        <Button
                          v-else-if="data.url?.toLowerCase().split('?', 1)[0].endsWith('.pdf')"
                          icon="pi pi-eye"
                          label="Preview"
                          size="small"
                          outlined
                          severity="secondary"
                          @click="openPdfPreview(data)"
                        />
                        <span v-else class="muted-text">-</span>
                      </div>
                    </template>
                  </Column>
                </DataTable>
              </div>

              <!-- Subsections Accordions -->
              <div v-if="Object.keys(section.subsections).length" class="subsection-container space-y-4">
                <Accordion
                  v-model:value="activeSubsectionIndexes[section.name]"
                  multiple
                  class="subsection-accordion"
                >
                  <AccordionPanel
                    v-for="(subEntries, subName) in section.subsections"
                    :key="subName"
                    :value="subName"
                  >
                    <AccordionHeader>
                      <div class="flex items-center justify-between w-full pr-4">
                        <span class="font-semibold text-sm md:text-base text-color">{{ subName }}</span>
                        <Tag :value="`${subEntries.length} items`" severity="secondary" class="ml-auto text-xs" />
                      </div>
                    </AccordionHeader>
                    <AccordionContent>
                      <div class="table-shell">
                        <DataTable :value="subEntries" striped-rows table-style="min-width: 50rem">
                          <Column field="description" header="Description">
                            <template #body="{ data }">
                              <a
                                v-if="data.url"
                                :href="data.url"
                                target="_blank"
                                rel="noopener noreferrer"
                                class="record-title"
                              >
                                {{ data.description }}
                              </a>
                              <span v-else>{{ data.description }}</span>
                            </template>
                          </Column>

                          <Column field="frequency" header="Frequency" style="width: 10rem" class="text-center">
                            <template #body="{ data }">
                              <Tag
                                v-if="data.frequency"
                                :value="data.frequency"
                                :severity="frequencySeverity(data.frequency)"
                              />
                              <span v-else class="muted-text">-</span>
                            </template>
                          </Column>

                          <Column field="format_type" header="Format" style="width: 6rem" class="text-center">
                            <template #body="{ data }">
                              <Button
                                v-if="data.format_type === 'xlsx' || data.format_type === 'xls'"
                                as="a"
                                :href="data.format_url"
                                target="_blank"
                                rel="noopener noreferrer"
                                icon="pi pi-file-excel"
                                text
                                rounded
                                severity="success"
                                title="Download Excel"
                              />
                              <Button
                                v-else-if="data.format_type === 'pdf'"
                                as="a"
                                :href="data.format_url"
                                target="_blank"
                                rel="noopener noreferrer"
                                icon="pi pi-file-pdf"
                                text
                                rounded
                                severity="danger"
                                title="Download PDF"
                              />
                              <span v-else class="muted-text">-</span>
                            </template>
                          </Column>

                          <Column field="last_update" header="Last Update" style="width: 10rem" class="text-center">
                            <template #body="{ data }">
                              <a
                                v-if="data.last_update?.toLowerCase().includes('easydata')"
                                href="https://easydata.sbp.org.pk"
                                target="_blank"
                                rel="noopener noreferrer"
                                class="easydata-link"
                              >
                                <i class="pi pi-database" /> EasyData
                              </a>
                              <span v-else-if="data.last_update">{{ data.last_update }}</span>
                              <span v-else class="muted-text">-</span>
                            </template>
                          </Column>

                          <Column field="archive_url" header="Archive" style="width: 8rem" class="text-center">
                            <template #body="{ data }">
                              <a
                                v-if="data.archive_url"
                                :href="data.archive_url"
                                target="_blank"
                                rel="noopener noreferrer"
                                class="archive-link"
                              >
                                <i class="pi pi-box" /> Archive
                              </a>
                              <span v-else class="muted-text">-</span>
                            </template>
                          </Column>

                          <Column header="Actions" style="width: 10rem" class="text-center">
                            <template #body="{ data }">
                              <div class="row-actions justify-center">
                                <Button
                                  v-if="data.can_summarize"
                                  icon="pi pi-sparkles"
                                  label="Summary"
                                  size="small"
                                  outlined
                                  severity="primary"
                                  @click="openSummary(data)"
                                />
                                <Button
                                  v-else-if="data.url?.toLowerCase().split('?', 1)[0].endsWith('.pdf')"
                                  icon="pi pi-eye"
                                  label="Preview"
                                  size="small"
                                  outlined
                                  severity="secondary"
                                  @click="openPdfPreview(data)"
                                />
                                <span v-else class="muted-text">-</span>
                              </div>
                            </template>
                          </Column>
                        </DataTable>
                      </div>
                    </AccordionContent>
                  </AccordionPanel>
                </Accordion>
              </div>
            </AccordionContent>
          </AccordionPanel>
        </Accordion>
      </div>
    </div>

    <!-- PDF Preview Dialog -->
    <PdfPreviewDialog
      v-model:visible="pdfDialogVisible"
      :title="pdfTitle"
      :url="pdfUrl"
    />

    <!-- AI Document Summary Dialog -->
    <Dialog
      v-model:visible="summaryDialogVisible"
      modal
      header="Document Intelligence Summary"
      :style="{ width: '50rem' }"
      :breakpoints="{ '960px': '75vw', '640px': '90vw' }"
    >
      <div class="summary-dialog-content">
        <div class="flex items-center justify-between gap-3 mb-4">
          <h3 class="text-sm md:text-base font-semibold m-0 text-color">{{ summaryTitle }}</h3>
          <Button
            as="a"
            :href="summaryUrl"
            target="_blank"
            rel="noopener noreferrer"
            icon="pi pi-external-link"
            label="Open Source"
            size="small"
            outlined
            class="flex-shrink-0"
          />
        </div>

        <div v-if="summaryLoading" class="flex flex-col items-center justify-center py-12 gap-3 text-muted-color">
          <ProgressSpinner />
          <span>Generating AI summary...</span>
        </div>

        <Message v-else-if="summaryError" severity="error" :closable="false">
          {{ summaryError }}
        </Message>

        <div
          v-else-if="summaryText"
          class="markdown-body summary-body mt-2 border border-surface rounded p-4 bg-surface-card"
          v-html="renderMarkdown(summaryText)"
        />

        <Message v-else severity="info" :closable="false">
          No summary was loaded.
        </Message>
      </div>
    </Dialog>
  </section>
</template>

<style scoped>
.eco-data-toolbar {
  display: grid;
  grid-template-columns: minmax(17rem, 1.35fr) minmax(0, 3fr) auto;
  align-items: center;
  gap: 0.5rem;
  padding: 0.15rem 0 0.55rem;
  border-bottom: 1px solid var(--sbp-border);
}

.eco-data-search {
  position: relative;
  display: flex;
  align-items: center;
}

.eco-data-search > .pi-search {
  position: absolute;
  z-index: 1;
  left: 0.75rem;
  color: var(--sbp-muted);
  pointer-events: none;
}

.eco-data-search :deep(.p-inputtext) {
  width: 100%;
  padding-left: 2.25rem;
  padding-right: 2.4rem;
}

.eco-data-search :deep(.p-button) {
  position: absolute;
  right: 0.15rem;
}

.toolbar-quick-links {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.35rem;
  min-width: 0;
}

.toolbar-quick-link {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.35rem;
  min-width: 0;
  min-height: 2.65rem;
  padding: 0.4rem 0.55rem;
  border: 1px solid var(--sbp-border);
  border-radius: 0.45rem;
  background: var(--sbp-surface);
  color: var(--sbp-text);
  font-size: 0.78rem;
  font-weight: 600;
  line-height: 1.15;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.toolbar-quick-link span {
  overflow: hidden;
  text-overflow: ellipsis;
}

.toolbar-quick-link .pi {
  color: var(--sbp-green);
  font-size: 0.7rem;
  flex: 0 0 auto;
}

.toolbar-quick-link:hover {
  border-color: var(--sbp-green);
  background: color-mix(in srgb, var(--sbp-green) 6%, var(--sbp-surface));
}

.toolbar-quick-link.is-disabled {
  pointer-events: none;
  color: var(--sbp-muted);
}

.eco-data-content {
  display: grid;
  gap: 0.5rem;
}

.eco-data-view :deep(.p-accordionheader) {
  min-height: 0;
  padding: 0.42rem 0.6rem;
}

.eco-data-view :deep(.p-accordioncontent-content) {
  padding: 0.35rem 0.45rem 0.45rem;
}

.section-accordion > :deep(.p-accordionpanel > .p-accordionheader) {
  border: 1px solid color-mix(in srgb, var(--sbp-green) 28%, var(--sbp-border));
  background: color-mix(in srgb, var(--sbp-green) 10%, var(--sbp-surface));
}

.section-accordion > :deep(.p-accordionpanel > .p-accordionheader:hover) {
  background: color-mix(in srgb, var(--sbp-green) 15%, var(--sbp-surface));
}

.subsection-accordion > :deep(.p-accordionpanel > .p-accordionheader) {
  border: 1px solid var(--sbp-border);
  background: color-mix(in srgb, var(--sbp-green) 5%, var(--sbp-surface));
}

.eco-data-view :deep(.p-datatable-header-cell),
.eco-data-view :deep(.p-datatable-tbody > tr > td) {
  padding: 0.32rem 0.5rem;
  font-size: 0.82rem;
  line-height: 1.25;
}

.eco-data-view :deep(.p-tag) {
  padding: 0.15rem 0.38rem;
  font-size: 0.7rem;
}

.eco-data-view :deep(.p-button-sm) {
  padding: 0.28rem 0.48rem;
  font-size: 0.76rem;
}

.eco-data-view .table-shell {
  border-radius: 0.45rem;
}

.eco-data-view .subsection-container {
  display: grid;
  gap: 0.35rem;
}

.easydata-link {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  color: var(--sbp-green);
  font-weight: 600;
}

.easydata-link:hover {
  text-decoration: underline;
}

.archive-link {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  color: var(--sbp-green);
  font-size: 0.85rem;
}

.archive-link:hover {
  text-decoration: underline;
}

.summary-body {
  max-height: 60vh;
  overflow-y: auto;
  line-height: 1.6;
}

@media (max-width: 760px) {
  .eco-data-toolbar {
    grid-template-columns: 1fr auto;
  }

  .toolbar-quick-links {
    grid-column: 1 / -1;
    grid-row: 2;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
