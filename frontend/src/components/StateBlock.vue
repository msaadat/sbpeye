<script setup lang="ts">
import Button from 'primevue/button'
import ProgressSpinner from 'primevue/progressspinner'

withDefaults(
  defineProps<{
    state?: 'loading' | 'error' | 'empty'
    title: string
    message?: string
    actionLabel?: string
    icon?: string
  }>(),
  {
    state: 'empty',
    message: '',
    actionLabel: '',
    icon: '',
  },
)

const emit = defineEmits<{
  action: []
}>()
</script>

<template>
  <section class="state-block" :class="`state-${state}`">
    <ProgressSpinner v-if="state === 'loading'" class="state-spinner" />
    <i
      v-else
      class="state-icon pi"
      :class="icon || (state === 'error' ? 'pi-exclamation-triangle' : 'pi-inbox')"
    />

    <div>
      <h2>{{ title }}</h2>
      <p v-if="message">{{ message }}</p>
    </div>

    <Button
      v-if="actionLabel"
      :label="actionLabel"
      size="small"
      severity="secondary"
      @click="emit('action')"
    />
  </section>
</template>
