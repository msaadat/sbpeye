<script setup lang="ts">
/**
 * How this instance is configured and what it is physically able to do.
 *
 * The provider settings and the tracing toggle moved here unchanged when the console
 * grew tabs. What is new is the environment card: the same build behaves differently on
 * a maintainer's machine and on the deployment — SBP unreachable, `docling` absent — and
 * an operator reading a capability as "off" wants to know that is by design.
 */
import { onMounted, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import Accordion from 'primevue/accordion'
import AccordionContent from 'primevue/accordioncontent'
import AccordionHeader from 'primevue/accordionheader'
import AccordionPanel from 'primevue/accordionpanel'
import Card from 'primevue/card'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import ToggleSwitch from 'primevue/toggleswitch'

import DeploymentAiSettings from '@/components/DeploymentAiSettings.vue'
import {
  getAdminEnvironment,
  getSettings,
  saveSettings,
  type AdminEnvironment,
  type SettingsPayload,
} from '@/lib/api'
import { useLlmDebugState } from '@/lib/useLlmDebugState'
import { formatBytes, formatCount } from './adminFormat'

const toast = useToast()
const { state: llmDebugState, refreshLlmDebugState } = useLlmDebugState()

// Which detail panels are open. A real model, not a literal `[]` on the Accordion:
// that binding hands the component a fresh empty array on every render, so a panel
// closes again the instant anything else on the page updates.
const openDetail = ref<string[]>([])

const settings = ref<SettingsPayload | null>(null)
const settingsError = ref('')
const llmDebugAllowed = ref(true)
const llmDebugEnabled = ref(false)
const debugSaving = ref(false)

const environment = ref<AdminEnvironment | null>(null)
const environmentError = ref('')
const environmentLoading = ref(false)

async function loadDebugSetting(): Promise<void> {
  try {
    const payload = await getSettings()
    settings.value = payload
    llmDebugAllowed.value = payload.llm_debug_allowed !== false
    llmDebugEnabled.value = !!payload.llm_debug_enabled
  } catch (error) {
    settingsError.value = (error as Error).message
  }
}

async function loadEnvironment(): Promise<void> {
  environmentLoading.value = true
  environmentError.value = ''
  try {
    environment.value = await getAdminEnvironment()
  } catch (error) {
    environmentError.value = (error as Error).message || 'Could not read the environment.'
  } finally {
    environmentLoading.value = false
  }
}

async function saveDebugSetting(value: boolean): Promise<void> {
  if (!settings.value) return
  debugSaving.value = true
  llmDebugEnabled.value = value
  try {
    const result = await saveSettings({ ...settings.value, llm_debug_enabled: value })
    settings.value = result.settings
    llmDebugEnabled.value = !!result.settings.llm_debug_enabled
    await refreshLlmDebugState()
    toast.add({
      severity: 'success',
      summary: value ? 'Tracing enabled' : 'Tracing disabled',
      life: 3000,
    })
  } catch (error) {
    llmDebugEnabled.value = !value
    toast.add({
      severity: 'error',
      summary: 'Could not save',
      detail: (error as Error).message,
      life: 6000,
    })
  } finally {
    debugSaving.value = false
  }
}

onMounted(() => {
  void loadDebugSetting()
  void loadEnvironment()
})
</script>

<template>
  <div class="admin-tab-body">
    <Card class="glass-panel">
      <template #title>Corpus generation provider</template>
      <template #content>
        <Message v-if="settingsError" severity="error" :closable="false">{{ settingsError }}</Message>
        <DeploymentAiSettings />
      </template>
    </Card>

    <Card class="glass-panel">
      <template #title>Storage and capabilities</template>
      <template #content>
        <div v-if="environmentLoading && !environment" class="tab-loading">
          <ProgressSpinner style="width: 2rem; height: 2rem" />
        </div>
        <Message v-else-if="environmentError" severity="error" :closable="false">
          {{ environmentError }}
        </Message>

        <template v-else-if="environment">
          <div class="two-column">
            <div>
              <h3 class="section-label">Capabilities</h3>
              <dl class="kv-list">
                <dt>Checklist generation</dt>
                <dd>
                  <template v-if="environment.capabilities.checklist_generation">Available</template>
                  <template v-else>
                    Unavailable — <code>docling</code> is an optional extra, left out of the
                    deployment image on purpose. Everything else works without it.
                  </template>
                </dd>
                <dt>Vector store</dt>
                <dd>
                  {{ environment.capabilities.vector_store }}
                  <span v-if="environment.capabilities.vector_store_chunks !== null" class="muted-text">
                    {{ formatCount(environment.capabilities.vector_store_chunks) }} chunks
                  </span>
                </dd>
                <dt>LLM tracing</dt>
                <dd>
                  {{ environment.capabilities.llm_debug_allowed ? 'Permitted' : 'Blocked by LLM_DEBUG_ALLOWED' }}
                </dd>
                <dt>EcoData refresh</dt>
                <dd>
                  <template v-if="environment.capabilities.ecodata_refresh_seconds === '0'">
                    Disabled. This is the right setting while SBP is unreachable — otherwise
                    the scheduler throws hourly into the logs for a scrape that cannot succeed.
                  </template>
                  <template v-else-if="environment.capabilities.ecodata_refresh_seconds">
                    Every {{ environment.capabilities.ecodata_refresh_seconds }} s
                  </template>
                  <template v-else>Default (3600 s)</template>
                </dd>
                <dt>Embedding</dt>
                <dd>
                  {{ environment.embedding.provider }}
                  <span class="mono muted-text">{{ environment.embedding.model }}</span>
                </dd>
              </dl>
            </div>

            <div>
              <h3 class="section-label">On disk</h3>
              <dl class="kv-list">
                <template v-for="entry in environment.databases" :key="entry.name">
                  <dt>{{ entry.name }} db</dt>
                  <dd>{{ formatBytes(entry.bytes) }}</dd>
                </template>
                <template v-for="tree in environment.file_trees" :key="tree.name">
                  <dt>{{ tree.name }}</dt>
                  <dd>
                    <template v-if="tree.exists">
                      {{ formatBytes(tree.bytes) }} · {{ formatCount(tree.files) }} files
                    </template>
                    <template v-else>not present</template>
                    <span v-if="!tree.deletable" class="muted-text">never pruned</span>
                  </dd>
                </template>
              </dl>
              <p class="field-hint">
                Only the cache may be deleted. The laws archive holds superseded editions
                that exist nowhere else — SBP replaces its PDFs in place and keeps no
                history.
              </p>
            </div>
          </div>

          <Accordion v-model:value="openDetail" multiple class="detail-accordion">
            <AccordionPanel value="paths">
              <AccordionHeader>
                Paths
                <span class="panel-count">
                  {{ environment.databases.length + environment.file_trees.length + 1 }}
                </span>
              </AccordionHeader>
              <AccordionContent>
                <dl class="kv-list">
                  <dt>Data root</dt>
                  <dd class="mono">{{ environment.data_root }}</dd>
                  <template v-for="entry in environment.databases" :key="entry.path">
                    <dt>{{ entry.name }} db</dt>
                    <dd class="mono">{{ entry.path }}</dd>
                  </template>
                  <template v-for="tree in environment.file_trees" :key="tree.path">
                    <dt>{{ tree.name }}</dt>
                    <dd class="mono">{{ tree.path }}</dd>
                  </template>
                </dl>
              </AccordionContent>
            </AccordionPanel>
          </Accordion>
        </template>
      </template>
    </Card>

    <Card class="glass-panel">
      <template #title>LLM debug tracing</template>
      <template #content>
        <div class="debug-setting-row">
          <div>
            <strong>Enable LLM tracing</strong>
            <p>Capture provider requests, responses, retries, tool activity, parsed output and errors.</p>
          </div>
          <ToggleSwitch
            :model-value="llmDebugEnabled"
            :disabled="debugSaving || !llmDebugAllowed || !settings"
            aria-label="Enable LLM tracing"
            @update:model-value="saveDebugSetting"
          />
        </div>

        <Message v-if="!llmDebugAllowed" severity="warn" :closable="false">
          Set <code>LLM_DEBUG_ALLOWED=true</code> on the server to allow tracing.
        </Message>
        <Message severity="warn" :closable="false">
          Traces contain full prompts, retrieved context, tool results and responses — including
          other users' chat turns. They are stored without automatic retention. This is why the
          setting and the console are both restricted to administrators.
        </Message>

        <div v-if="llmDebugState.effective" class="chip-row">
          <RouterLink to="/debug">Open debug console</RouterLink>
          <span v-if="llmDebugState.trace_count !== undefined" class="muted-text">
            {{ llmDebugState.trace_count.toLocaleString() }} traces ·
            {{ ((llmDebugState.payload_bytes || 0) / 1024).toFixed(1) }} KiB
          </span>
        </div>
      </template>
    </Card>
  </div>
</template>

<style scoped>
.debug-setting-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1rem;
}

.debug-setting-row p {
  margin: 0.25rem 0 0;
  color: var(--sbp-muted);
  font-size: 0.8125rem;
}
</style>
