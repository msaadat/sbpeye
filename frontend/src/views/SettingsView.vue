<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Card from 'primevue/card'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Password from 'primevue/password'
import Select from 'primevue/select'
import { getMyAiSettings, saveMyAiSettings } from '@/lib/api'

/**
 * The signed-in user's own AI credentials, and nothing else.
 *
 * Deployment-wide provider configuration used to share this page and now lives in the
 * admin console (`AdminView`). Two configurations on one screen read as a duplicate —
 * they look identical and differ only in scope — and the one a tester can act on was the
 * smaller of the two. Splitting them puts each next to the thing it governs: your key
 * beside your chat, the deployment's key beside the corpus work it pays for.
 */
const toast = useToast()

const providerOptions = [
  { name: 'Mistral AI', value: 'mistral' },
  { name: 'OpenAI', value: 'openai' },
  { name: 'Groq', value: 'groq' },
  { name: 'OpenRouter', value: 'openrouter' },
  { name: 'Google Gemini', value: 'google' },
  { name: 'Ollama', value: 'ollama' },
  { name: 'LM Studio (local)', value: 'lmstudio' },
  { name: 'Custom', value: 'custom' },
]

// The signed-in user's own credentials, which is what chat runs on. Separate from
// everything else on this page: the rest is deployment configuration and belongs to the
// admin, while this is the key the user personally pays with.
const myProvider = ref('mistral')
const myBaseUrl = ref('')
const myModel = ref('')
const myChatModel = ref('')
const myApiKey = ref('')
const myApiKeySet = ref(false)
const mySaving = ref(false)
const myError = ref('')

async function loadMyAiSettings(): Promise<void> {
  try {
    const mine = await getMyAiSettings()
    myProvider.value = mine.provider || 'mistral'
    myBaseUrl.value = mine.base_url || ''
    myModel.value = mine.model || ''
    myChatModel.value = mine.chat_model || ''
    myApiKeySet.value = mine.api_key_set
  } catch (error) {
    myError.value = (error as Error).message
  }
}

async function saveMyAi(): Promise<void> {
  mySaving.value = true
  myError.value = ''
  try {
    const result = await saveMyAiSettings({
      provider: myProvider.value,
      base_url: myBaseUrl.value,
      model: myModel.value,
      chat_model: myChatModel.value,
      // Omitted when blank, so saving a model change does not wipe a stored key. The
      // server treats an explicit empty string as "clear it".
      ...(myApiKey.value ? { api_key: myApiKey.value } : {}),
    })
    myApiKeySet.value = result.api_key_set
    myApiKey.value = ''
    toast.add({ severity: 'success', summary: 'Your AI settings were saved', life: 3000 })
  } catch (error) {
    myError.value = (error as Error).message
  } finally {
    mySaving.value = false
  }
}

async function clearMyApiKey(): Promise<void> {
  myApiKey.value = ''
  mySaving.value = true
  try {
    const result = await saveMyAiSettings({ provider: myProvider.value, api_key: '' })
    myApiKeySet.value = result.api_key_set
    toast.add({ severity: 'success', summary: 'Your API key was removed', life: 3000 })
  } catch (error) {
    myError.value = (error as Error).message
  } finally {
    mySaving.value = false
  }
}

onMounted(() => {
  void loadMyAiSettings()
})
</script>

<template>
  <section class="view-stack">
    <div class="page-heading">
      <div>
        <p>Settings</p>
        <h1>Your AI provider</h1>
      </div>
    </div>

    <Card class="glass-panel">
      <template #content>
        <p class="field-hint">
          Chat runs on your own provider credentials on this deployment, so usage is billed to
          you rather than shared. Nothing here affects anyone else.
        </p>
        <Message v-if="myError" severity="error" :closable="false">{{ myError }}</Message>

        <div class="settings-grid">
          <label>
            Provider
            <Select v-model="myProvider" :options="providerOptions" option-label="name" option-value="value" />
          </label>
          <label>
            Model
            <InputText v-model="myModel" placeholder="Provider default" />
          </label>
          <label>
            Chat model <span class="optional">optional</span>
            <InputText v-model="myChatModel" placeholder="Same as model" />
          </label>
          <label>
            Base URL <span class="optional">optional</span>
            <InputText v-model="myBaseUrl" placeholder="Provider default" />
          </label>
          <label>
            API key
            <Password
              v-model="myApiKey"
              :feedback="false"
              toggle-mask
              :placeholder="myApiKeySet ? 'Stored — leave blank to keep' : 'Required for hosted providers'"
              autocomplete="off"
            />
          </label>
        </div>

        <div class="button-row">
          <Button label="Save my settings" icon="pi pi-save" :loading="mySaving" @click="saveMyAi" />
          <Button
            v-if="myApiKeySet"
            label="Remove my key"
            icon="pi pi-times"
            severity="secondary"
            text
            :loading="mySaving"
            @click="clearMyApiKey"
          />
          <span v-if="myApiKeySet" class="field-hint">A key is stored. It is never shown again.</span>
        </div>
      </template>
    </Card>
  </section>
</template>

<style scoped>
.field-hint {
  margin: 0 0 1rem;
  color: var(--text-muted, #6b7280);
  font-size: 0.8125rem;
}

.optional {
  color: var(--text-muted, #6b7280);
  font-weight: 400;
}
</style>
