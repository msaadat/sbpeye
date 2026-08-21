<script setup lang="ts">
/**
 * Accounts. Extracted verbatim from the single-page admin view when it grew tabs —
 * behaviour is unchanged, including the two delete guards.
 */
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

import { createUser, deleteUser, listUsers, type AdminUser } from '@/lib/api'
import { useCurrentUser } from '@/lib/useCurrentUser'
import { formatDate } from './adminFormat'

const toast = useToast()
const { user: currentUser } = useCurrentUser()

const users = ref<AdminUser[]>([])
const usersLoading = ref(false)
const usersError = ref('')

const newEmail = ref('')
const newPassword = ref('')
const newIsAdmin = ref(false)
const creating = ref(false)

const adminCount = computed(() => users.value.filter((u) => u.is_admin).length)

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

onMounted(loadUsers)
</script>

<template>
  <div class="admin-tab-body">
    <Card class="glass-panel">
      <template #title>Users</template>
      <template #content>
        <Message v-if="usersError" severity="error" :closable="false">{{ usersError }}</Message>

        <DataTable :value="users" :loading="usersLoading" size="small" class="facet-table">
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
  </div>
</template>

<style scoped>
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
  border: 1px solid var(--sbp-border);
}

.role-chip.is-admin {
  border-color: transparent;
  background: var(--p-primary-color, #2563eb);
  color: #fff;
}
</style>
