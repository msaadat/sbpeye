<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Password from 'primevue/password'
import ToggleSwitch from 'primevue/toggleswitch'

import {
  createUser,
  deleteUser,
  getSettings,
  listUsers,
  saveSettings,
  type AdminUser,
  type SettingsPayload,
} from '@/lib/api'
import DeploymentAiSettings from '@/components/DeploymentAiSettings.vue'
import { useCurrentUser } from '@/lib/useCurrentUser'
import { useLlmDebugState } from '@/lib/useLlmDebugState'

const toast = useToast()
const { user: currentUser, isAdmin, load: loadCurrentUser } = useCurrentUser()
const { state: llmDebugState, refreshLlmDebugState } = useLlmDebugState()

const users = ref<AdminUser[]>([])
const usersLoading = ref(false)
const usersError = ref('')

const newEmail = ref('')
const newPassword = ref('')
const newIsAdmin = ref(false)
const creating = ref(false)

const settings = ref<SettingsPayload | null>(null)
const llmDebugAllowed = ref(true)
const llmDebugEnabled = ref(false)
const debugSaving = ref(false)

const adminCount = computed(() => users.value.filter((u) => u.is_admin).length)

function formatDate(value?: string | null): string {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString()
}

async function loadUsers(): Promise<void> {
  usersLoading.value = true
  usersError.value = ''
  try {
    users.value = await listUsers()
  } catch (error) {
    usersError.value = (error as Error).message || 'Could not load users.'
  } finally {
    usersLoading.value = false
  }
}

async function loadDebugSetting(): Promise<void> {
  try {
    const payload = await getSettings()
    settings.value = payload
    llmDebugAllowed.value = payload.llm_debug_allowed !== false
    llmDebugEnabled.value = !!payload.llm_debug_enabled
  } catch (error) {
    // Non-fatal: user management is the point of this page and still works.
    usersError.value = usersError.value || (error as Error).message
  }
}

async function addUser(): Promise<void> {
  creating.value = true
  try {
    const created = await createUser({
      email: newEmail.value,
      password: newPassword.value,
      is_admin: newIsAdmin.value,
    })
    toast.add({
      severity: 'success',
      summary: 'User created',
      detail: `${created.email} can now sign in.`,
      life: 4000,
    })
    newEmail.value = ''
    newPassword.value = ''
    newIsAdmin.value = false
    await loadUsers()
  } catch (error) {
    toast.add({
      severity: 'error',
      summary: 'Could not create user',
      detail: (error as Error).message,
      life: 6000,
    })
  } finally {
    creating.value = false
  }
}

async function removeUser(target: AdminUser): Promise<void> {
  // Deliberately a confirm rather than a soft delete: this takes the account and every
  // chat conversation it owns, and there is no undo.
  const ok = window.confirm(
    `Delete ${target.email}? Their chat history goes with them. This cannot be undone.`,
  )
  if (!ok) return

  try {
    await deleteUser(target.id)
    toast.add({ severity: 'success', summary: 'User deleted', detail: target.email, life: 4000 })
    await loadUsers()
  } catch (error) {
    toast.add({
      severity: 'error',
      summary: 'Could not delete user',
      detail: (error as Error).message,
      life: 6000,
    })
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

onMounted(async () => {
  await loadCurrentUser()
  if (!isAdmin.value) return
  await Promise.all([loadUsers(), loadDebugSetting()])
})
</script>

<template>
  <div class="admin-view">
    <header class="admin-header">
      <h1>Admin console</h1>
      <p>Accounts and deployment diagnostics.</p>
    </header>

    <!--
      The server is the authority here: every route this page calls is admin-gated and
      returns 403 regardless. This is so a non-admin who types the URL reads an
      explanation rather than a page of failed requests.
    -->
    <Message v-if="!isAdmin" severity="warn" :closable="false">
      This page is for administrators. Signed in as {{ currentUser?.email }}.
    </Message>

    <template v-else>
      <Card class="glass-panel">
        <template #title>Users</template>
        <template #content>
          <Message v-if="usersError" severity="error" :closable="false">{{ usersError }}</Message>

          <DataTable :value="users" :loading="usersLoading" size="small" class="users-table">
            <Column field="email" header="Email" />
            <Column header="Role">
              <template #body="{ data }">
                <span class="role-chip" :class="{ 'is-admin': data.is_admin }">
                  {{ data.is_admin ? 'Admin' : 'Tester' }}
                </span>
              </template>
            </Column>
            <Column header="Last signed in">
              <template #body="{ data }">{{ formatDate(data.last_login_at) }}</template>
            </Column>
            <Column header="Created">
              <template #body="{ data }">{{ formatDate(data.created_at) }}</template>
            </Column>
            <Column header="">
              <template #body="{ data }">
                <Button
                  text
                  rounded
                  severity="danger"
                  icon="pi pi-trash"
                  :aria-label="`Delete ${data.email}`"
                  :title="
                    data.id === currentUser?.id
                      ? 'You cannot delete your own account'
                      : data.is_admin && adminCount < 2
                        ? 'This is the only administrator'
                        : `Delete ${data.email}`
                  "
                  :disabled="data.id === currentUser?.id || (data.is_admin && adminCount < 2)"
                  @click="removeUser(data)"
                />
              </template>
            </Column>
            <template #empty>No users yet.</template>
          </DataTable>
        </template>
      </Card>

      <Card class="glass-panel">
        <template #title>Add a user</template>
        <template #content>
          <p class="field-hint">
            There is no self-registration on this deployment: chat runs on each account's own
            provider key, and accounts are created here so the list stays short and known.
          </p>
          <form class="add-user-form" @submit.prevent="addUser">
            <label>
              Email
              <InputText v-model="newEmail" type="email" required autocomplete="off" />
            </label>
            <label>
              Password
              <Password
                v-model="newPassword"
                :feedback="false"
                toggle-mask
                required
                autocomplete="new-password"
              />
            </label>
            <label class="inline-toggle">
              <ToggleSwitch v-model="newIsAdmin" aria-label="Grant administrator access" />
              <span>Administrator</span>
            </label>
            <Button
              type="submit"
              label="Create user"
              icon="pi pi-user-plus"
              :loading="creating"
              :disabled="!newEmail || !newPassword"
            />
          </form>
        </template>
      </Card>

      <Card class="glass-panel">
        <template #title>Corpus generation provider</template>
        <template #content>
          <DeploymentAiSettings />
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

          <div v-if="llmDebugState.effective" class="button-row">
            <RouterLink to="/debug">Open debug console</RouterLink>
            <span v-if="llmDebugState.trace_count !== undefined">
              {{ llmDebugState.trace_count.toLocaleString() }} traces ·
              {{ ((llmDebugState.payload_bytes || 0) / 1024).toFixed(1) }} KiB
            </span>
          </div>
        </template>
      </Card>
    </template>
  </div>
</template>

<style scoped>
.admin-view {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
  padding: 1.5rem;
  max-width: 60rem;
}

.admin-header h1 {
  margin: 0;
  font-size: 1.35rem;
}

.admin-header p {
  margin: 0.25rem 0 0;
  color: var(--text-muted, #6b7280);
  font-size: 0.875rem;
}

.field-hint {
  margin: 0 0 1rem;
  color: var(--text-muted, #6b7280);
  font-size: 0.8125rem;
}

.add-user-form {
  display: flex;
  flex-wrap: wrap;
  gap: 1rem;
  align-items: flex-end;
}

.add-user-form label {
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
  font-size: 0.8125rem;
}

.inline-toggle {
  flex-direction: row !important;
  align-items: center;
  gap: 0.5rem;
  padding-bottom: 0.5rem;
}

.role-chip {
  font-size: 0.75rem;
  padding: 0.125rem 0.5rem;
  border-radius: 999px;
  border: 1px solid var(--surface-border, #d1d5db);
}

.role-chip.is-admin {
  border-color: transparent;
  background: var(--p-primary-color, #2563eb);
  color: #fff;
}

.debug-setting-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1rem;
}

.debug-setting-row p {
  margin: 0.25rem 0 0;
  color: var(--text-muted, #6b7280);
  font-size: 0.8125rem;
}

.button-row {
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-top: 1rem;
  font-size: 0.8125rem;
}
</style>
