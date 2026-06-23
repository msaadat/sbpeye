<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Card from 'primevue/card'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Password from 'primevue/password'
import Select from 'primevue/select'
import { getSettings, saveSettings, testEmbeddingConnection, testSettingsConnection } from '@/lib/api'

type ProviderOption = {
  name: string
  value: string
  baseUrl: string
  apiKeyEnvVar: string
}

const toast = useToast()

const loading = ref(true)
const saving = ref(false)
const testing = ref(false)
const testingEmbeddings = ref(false)
const loadError = ref('')

const provider = ref('lmstudio')
const baseUrl = ref('http://localhost:1234/v1')
const apiKey = ref('')
const apiKeyConfigured = ref(false)
const apiKeyEnvVar = ref('AI_API_KEY')
const clearApiKey = ref(false)
const model = ref('local-model')
const chatModel = ref('')
const maxContextTokens = ref(4000)

const embeddingProvider = ref('fastembed')
const embeddingModel = ref('BAAI/bge-base-en-v1.5')
const embeddingBaseUrl = ref('http://localhost:1234/v1')
const embeddingApiKey = ref('')
const embeddingApiKeyConfigured = ref(false)
const embeddingApiKeyEnvVar = ref('EMBEDDING_API_KEY')
const clearEmbeddingApiKey = ref(false)
const managedEnvFile = ref('.env.local')

const providerOptions: ProviderOption[] = [
  { name: 'LM Studio (Local)', value: 'lmstudio', baseUrl: 'http://localhost:1234/v1', apiKeyEnvVar: 'AI_API_KEY' },
  { name: 'OpenAI', value: 'openai', baseUrl: 'https://api.openai.com/v1', apiKeyEnvVar: 'OPENAI_API_KEY' },
  { name: 'Google Gemini', value: 'google', baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai/', apiKeyEnvVar: 'GEMINI_API_KEY' },
  { name: 'Groq', value: 'groq', baseUrl: 'https://api.groq.com/openai/v1', apiKeyEnvVar: 'GROQ_API_KEY' },
  { name: 'OpenRouter', value: 'openrouter', baseUrl: 'https://openrouter.ai/api/v1', apiKeyEnvVar: 'OPENROUTER_API_KEY' },
  { name: 'Custom OpenAI-Compatible', value: 'custom', baseUrl: 'http://localhost:1234/v1', apiKeyEnvVar: 'AI_API_KEY' },
]

const embeddingProviderOptions = [
  { name: 'FastEmbed (Local)', value: 'fastembed' },
  { name: 'LM Studio', value: 'lmstudio' },
]

const providerMeta = Object.fromEntries(providerOptions.map((option) => [option.value, option])) as Record<string, ProviderOption>

const selectedProviderMeta = computed(() => providerMeta[provider.value] ?? providerMeta.custom)
const externalProvider = computed(() => provider.value !== 'lmstudio')
const embeddingNeedsApiKey = computed(() => embeddingProvider.value !== 'fastembed')

watch(provider, (next, previous) => {
  const nextMeta = providerMeta[next] ?? providerMeta.custom
  const previousMeta = providerMeta[previous] ?? providerMeta.custom

  if (!baseUrl.value || baseUrl.value === previousMeta.baseUrl) {
    baseUrl.value = nextMeta.baseUrl
  }

  apiKeyEnvVar.value = nextMeta.apiKeyEnvVar
  apiKeyConfigured.value = false
  apiKey.value = ''
  clearApiKey.value = false
})

watch(embeddingProvider, (next) => {
  embeddingApiKeyConfigured.value = false
  if (next === 'fastembed') {
    clearEmbeddingApiKey.value = false
    embeddingApiKey.value = ''
  }
})

watch(apiKey, (value) => {
  if (value.trim()) {
    clearApiKey.value = false
  }
})

watch(embeddingApiKey, (value) => {
  if (value.trim()) {
    clearEmbeddingApiKey.value = false
  }
})

async function loadSettings() {
  loading.value = true
  loadError.value = ''
  try {
    const settings = await getSettings()
    provider.value = settings.provider
    baseUrl.value = settings.base_url
    apiKey.value = ''
    apiKeyConfigured.value = !!settings.api_key_configured
    apiKeyEnvVar.value = settings.api_key_env_var || selectedProviderMeta.value.apiKeyEnvVar
    clearApiKey.value = false
    model.value = settings.model
    chatModel.value = settings.chat_model || ''
    maxContextTokens.value = settings.max_context_tokens
    embeddingProvider.value = settings.embedding_provider
    embeddingModel.value = settings.embedding_model
    embeddingBaseUrl.value = settings.embedding_base_url
    embeddingApiKey.value = ''
    embeddingApiKeyConfigured.value = !!settings.embedding_api_key_configured
    embeddingApiKeyEnvVar.value = settings.embedding_api_key_env_var || 'EMBEDDING_API_KEY'
    clearEmbeddingApiKey.value = false
    managedEnvFile.value = settings.managed_env_file || '.env.local'
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : 'Failed to load settings'
  } finally {
    loading.value = false
  }
}

async function handleSave() {
  saving.value = true
  try {
    const result = await saveSettings({
      provider: provider.value,
      base_url: baseUrl.value,
      api_key: apiKey.value,
      clear_api_key: clearApiKey.value,
      model: model.value,
      chat_model: chatModel.value,
      max_context_tokens: maxContextTokens.value,
      embedding_provider: embeddingProvider.value,
      embedding_model: embeddingModel.value,
      embedding_base_url: embeddingBaseUrl.value,
      embedding_api_key: embeddingApiKey.value,
      clear_embedding_api_key: clearEmbeddingApiKey.value,
    })

    apiKey.value = ''
    apiKeyConfigured.value = !!result.settings.api_key_configured
    apiKeyEnvVar.value = result.settings.api_key_env_var || selectedProviderMeta.value.apiKeyEnvVar
    clearApiKey.value = false
    maxContextTokens.value = result.settings.max_context_tokens
    embeddingApiKey.value = ''
    embeddingApiKeyConfigured.value = !!result.settings.embedding_api_key_configured
    embeddingApiKeyEnvVar.value = result.settings.embedding_api_key_env_var || 'EMBEDDING_API_KEY'
    clearEmbeddingApiKey.value = false
    managedEnvFile.value = result.settings.managed_env_file || '.env.local'

    toast.add({ severity: 'success', summary: 'Saved', detail: result.message, life: 3000 })
  } catch (err) {
    const msg = err instanceof Error ? err.message : 'Unknown error'
    toast.add({ severity: 'error', summary: 'Save failed', detail: msg, life: 5000 })
  } finally {
    saving.value = false
  }
}

async function handleEmbeddingTest() {
  testingEmbeddings.value = true
  try {
    const result = (await testEmbeddingConnection()) as { success?: boolean; error?: string; dimensions?: number }
    if (result.success) {
      toast.add({ severity: 'success', summary: 'Embedding test', detail: `Connection successful (${result.dimensions} dimensions)`, life: 3000 })
    } else {
      toast.add({ severity: 'error', summary: 'Embedding test', detail: result.error || 'Connection failed', life: 5000 })
    }
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Embedding test', detail: err instanceof Error ? err.message : 'Request failed', life: 5000 })
  } finally {
    testingEmbeddings.value = false
  }
}

async function handleTest() {
  testing.value = true
  try {
    const result = (await testSettingsConnection()) as { success?: boolean; error?: string; response?: string }
    if (result.success) {
      toast.add({ severity: 'success', summary: 'Connection test', detail: 'Connection successful', life: 3000 })
    } else {
      toast.add({ severity: 'error', summary: 'Connection test', detail: result.error || 'Connection failed', life: 5000 })
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : 'Request failed'
    toast.add({ severity: 'error', summary: 'Connection test', detail: msg, life: 5000 })
  } finally {
    testing.value = false
  }
}

function markApiKeyForRemoval() {
  clearApiKey.value = true
  apiKey.value = ''
}

function markEmbeddingKeyForRemoval() {
  clearEmbeddingApiKey.value = true
  embeddingApiKey.value = ''
}

onMounted(() => {
  void loadSettings()
})
</script>

<template>
  <section class="view-stack">
    <div class="page-heading">
      <div>
        <p>Settings</p>
        <h1>AI provider configuration</h1>
      </div>
      <div class="button-row">
        <Button
          label="Test"
          icon="pi pi-bolt"
          :loading="testing"
          :disabled="loading || saving"
          @click="handleTest"
        />
        <Button
          label="Save"
          icon="pi pi-save"
          :loading="saving"
          :disabled="loading"
          @click="handleSave"
        />
      </div>
    </div>

    <Message v-if="loadError" severity="error" :closable="false">
      {{ loadError }}
    </Message>

    <Card class="glass-panel">
      <template #content>
        <div class="settings-grid">
          <label>
            <span>Provider</span>
            <Select
              v-model="provider"
              :options="providerOptions"
              option-label="name"
              option-value="value"
              placeholder="Select provider"
              :disabled="loading"
            />
          </label>
          <label>
            <span>Base URL</span>
            <InputText
              v-model="baseUrl"
              :placeholder="selectedProviderMeta.baseUrl"
              :disabled="loading"
            />
          </label>
          <label>
            <span>API key</span>
            <Password
              v-model="apiKey"
              :feedback="false"
              toggle-mask
              :placeholder="externalProvider ? 'Enter a new token to replace the saved one' : 'Optional for LM Studio'"
              :disabled="loading"
            />
          </label>
          <label>
            <span>Default model</span>
            <InputText
              v-model="model"
              placeholder="Enter the provider model ID"
              :disabled="loading"
            />
          </label>
          <label>
            <span>Chat model</span>
            <InputText
              v-model="chatModel"
              placeholder="Leave blank to reuse default model"
              :disabled="loading"
            />
          </label>
          <label>
            <span>Max context tokens (auto-detected when available)</span>
            <InputNumber
              v-model="maxContextTokens"
              :min="500"
              :max="32000"
              :disabled="loading"
            />
          </label>
        </div>
        <Message severity="secondary" :closable="false">
          Token env var: <code>{{ apiKeyEnvVar }}</code>. <span v-if="apiKeyConfigured && !clearApiKey">A token is currently saved. Leave the field blank to keep it.</span><span v-else-if="clearApiKey">The saved token will be removed on the next save.</span><span v-else>No token is currently saved for this provider.</span>
        </Message>
        <Message severity="success" :closable="false">
          Saved LLM provider, URL, model, and token changes apply to the next request. No server restart is required.
        </Message>
        <div class="button-row">
          <Button
            v-if="apiKeyConfigured && !clearApiKey"
            label="Remove saved token"
            severity="secondary"
            text
            :disabled="loading || saving"
            @click="markApiKeyForRemoval"
          />
        </div>
      </template>
    </Card>

    <Card class="glass-panel">
      <template #title>Search embeddings</template>
      <template #content>
        <div class="settings-grid">
          <label>
            <span>Embedding provider</span>
            <Select v-model="embeddingProvider" :options="embeddingProviderOptions" option-label="name" option-value="value" :disabled="loading" />
          </label>
          <label>
            <span>Embedding model</span>
            <InputText v-model="embeddingModel" placeholder="BAAI/bge-base-en-v1.5" :disabled="loading" />
          </label>
          <label>
            <span>Embedding base URL</span>
            <InputText v-model="embeddingBaseUrl" placeholder="http://localhost:1234/v1" :disabled="loading || embeddingProvider !== 'lmstudio'" />
          </label>
          <label>
            <span>Embedding API key</span>
            <Password
              v-model="embeddingApiKey"
              :feedback="false"
              toggle-mask
              :placeholder="embeddingNeedsApiKey ? 'Enter a new token to replace the saved one' : 'Not used by FastEmbed'"
              :disabled="loading || !embeddingNeedsApiKey"
            />
          </label>
        </div>
        <Message severity="secondary" :closable="false">
          Embedding token env var: <code>{{ embeddingApiKeyEnvVar }}</code>. <span v-if="embeddingProvider === 'fastembed'">FastEmbed does not use an API key.</span><span v-else-if="embeddingApiKeyConfigured && !clearEmbeddingApiKey">A token is currently saved. Leave the field blank to keep it.</span><span v-else-if="clearEmbeddingApiKey">The saved embedding token will be removed on the next save.</span><span v-else>No embedding token is currently saved.</span>
        </Message>
        <Message severity="warn" :closable="false">
          Run <code>sbpeye reindex</code> after changing the embedding provider or model so stored vectors remain compatible. No server restart is required.
        </Message>
        <div class="button-row">
          <Button
            v-if="embeddingProvider !== 'fastembed' && embeddingApiKeyConfigured && !clearEmbeddingApiKey"
            label="Remove saved embedding token"
            severity="secondary"
            text
            :disabled="loading || saving"
            @click="markEmbeddingKeyForRemoval"
          />
          <Button label="Test embeddings" icon="pi pi-bolt" :loading="testingEmbeddings" :disabled="loading || saving" @click="handleEmbeddingTest" />
        </div>
      </template>
    </Card>
  </section>
</template>
