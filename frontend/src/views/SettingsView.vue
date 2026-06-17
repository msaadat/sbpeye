<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Card from 'primevue/card'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Password from 'primevue/password'
import Select from 'primevue/select'
import { getSettings, saveSettings, testSettingsConnection } from '@/lib/api'

const toast = useToast()

const loading = ref(true)
const saving = ref(false)
const testing = ref(false)
const loadError = ref('')

const provider = ref('lmstudio')
const baseUrl = ref('http://localhost:1234/v1')
const apiKey = ref('lm-studio')
const model = ref('local-model')
const chatModel = ref('')
const maxContextTokens = ref(4000)

const providerOptions = [
  { name: 'LM Studio (Local)', value: 'lmstudio' },
  { name: 'OpenAI', value: 'openai' },
  { name: 'Google Gemini', value: 'google' },
]

async function loadSettings() {
  loading.value = true
  loadError.value = ''
  try {
    const settings = await getSettings()
    provider.value = settings.provider
    baseUrl.value = settings.base_url
    apiKey.value = settings.api_key
    model.value = settings.model
    chatModel.value = settings.chat_model || ''
    maxContextTokens.value = settings.max_context_tokens
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
      model: model.value,
      chat_model: chatModel.value,
      max_context_tokens: maxContextTokens.value,
    })
    toast.add({ severity: 'success', summary: 'Saved', detail: result.message, life: 3000 })
  } catch (err) {
    const msg = err instanceof Error ? err.message : 'Unknown error'
    toast.add({ severity: 'error', summary: 'Save failed', detail: msg, life: 5000 })
  } finally {
    saving.value = false
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

    <Card>
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
              placeholder="http://localhost:1234/v1"
              :disabled="loading"
            />
          </label>
          <label>
            <span>API key</span>
            <Password
              v-model="apiKey"
              :feedback="false"
              toggle-mask
              placeholder="sk-... or lm-studio"
              :disabled="loading"
            />
          </label>
          <label>
            <span>Default model</span>
            <InputText
              v-model="model"
              placeholder="gpt-4o-mini, gemini-1.5-flash, local-model"
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
            <span>Max context tokens</span>
            <InputNumber
              v-model="maxContextTokens"
              :min="500"
              :max="32000"
              :disabled="loading"
            />
          </label>
        </div>
      </template>
    </Card>
  </section>
</template>
