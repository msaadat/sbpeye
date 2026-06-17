<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import ScrollPanel from 'primevue/scrollpanel'
import Sidebar from 'primevue/sidebar'
import Tag from 'primevue/tag'
import StateBlock from '@/components/StateBlock.vue'
import { getSbpNews, getCircularByUrl, type SbpNewsResponse } from '@/lib/api'

const router = useRouter()
const visible = ref(false)
const news = ref<SbpNewsResponse | null>(null)
const loading = ref(false)
const error = ref('')
const resolving = ref<string | null>(null)

async function loadNews() {
  loading.value = true
  error.value = ''
  news.value = null
  try {
    news.value = await getSbpNews()
    if (news.value.error) {
      error.value = news.value.error
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to load SBP news'
  } finally {
    loading.value = false
  }
}

function isCircularLink(url: string): boolean {
  return url.startsWith('/view_circular?cir=')
}

function extractCircularUrl(url: string): string {
  const params = new URLSearchParams(url.split('?')[1])
  return params.get('cir') || ''
}

function openExternal(url: string) {
  if (isCircularLink(url)) {
    resolveAndNavigate(url)
  } else {
    window.open(url, '_blank', 'noopener noreferrer')
  }
}

async function resolveAndNavigate(viewUrl: string) {
  const cirUrl = extractCircularUrl(viewUrl)
  if (!cirUrl) return
  resolving.value = cirUrl
  try {
    const circular = await getCircularByUrl(cirUrl)
    if (circular?.id) {
      visible.value = false
      await router.push(`/circulars/${circular.id}`)
    }
  } catch {
    window.open(cirUrl, '_blank', 'noopener noreferrer')
  } finally {
    resolving.value = null
  }
}

watch(visible, async (isOpen) => {
  if (isOpen && !news.value && !loading.value) {
    await loadNews()
  }
})
</script>

<template>
  <div class="sbp-news-trigger">
    <Button
      icon="pi pi-megaphone"
      text
      rounded
      aria-label="SBP News"
      @click="visible = true"
    />
    <Sidebar
      v-model:visible="visible"
      position="right"
      header="SBP News & Press Releases"
      class="sbp-news-sidebar"
      :pt="{
        root: { class: 'news-sidebar-root' },
        header: { class: 'news-sidebar-header' },
        content: { class: 'news-sidebar-content' },
      }"
    >
      <StateBlock
        v-if="loading"
        state="loading"
        title="Loading news..."
      />
      <StateBlock
        v-else-if="error"
        state="error"
        title="Unable to load news"
        :message="error"
        icon="pi pi-exclamation-triangle"
      />
      <StateBlock
        v-else-if="!news || (news.press_releases.length === 0 && news.whats_new.length === 0)"
        state="empty"
        title="No news available"
        message="SBP press releases and What's New will appear here."
        icon="pi pi-megaphone"
      />
      <ScrollPanel v-else class="news-scroll">
        <section v-if="news.press_releases.length > 0" class="news-section">
          <h3 class="news-section-title">
            Press Releases
            <Tag :value="news.press_releases.length" severity="info" />
          </h3>
          <ul class="news-list">
            <li v-for="(item, idx) in news.press_releases" :key="'pr-' + idx" class="news-item">
              <i class="pi pi-external-link news-item-icon" />
              <a
                class="news-link"
                :class="{ 'circular-link': isCircularLink(item.url) }"
                :href="isCircularLink(item.url) ? '#' : item.url"
                :target="isCircularLink(item.url) ? undefined : '_blank'"
                :rel="isCircularLink(item.url) ? undefined : 'noopener noreferrer'"
                :title="item.title"
                @click.prevent="openExternal(item.url)"
              >
                <span class="news-title">{{ item.title }}</span>
                <i
                  v-if="isCircularLink(item.url)"
                  class="pi pi-arrow-right news-arrow"
                  :class="{ 'pi-spin pi-spinner': resolving === extractCircularUrl(item.url) }"
                />
              </a>
            </li>
          </ul>
        </section>

        <section v-if="news.whats_new.length > 0" class="news-section">
          <h3 class="news-section-title">
            What's New
            <Tag :value="news.whats_new.length" severity="warn" />
          </h3>
          <ul class="news-list">
            <li v-for="(item, idx) in news.whats_new" :key="'wn-' + idx" class="news-item">
              <i class="pi pi-star news-item-icon" />
              <a
                class="news-link"
                :class="{ 'circular-link': isCircularLink(item.url) }"
                :href="isCircularLink(item.url) ? '#' : item.url"
                :target="isCircularLink(item.url) ? undefined : '_blank'"
                :rel="isCircularLink(item.url) ? undefined : 'noopener noreferrer'"
                :title="item.title"
                @click.prevent="openExternal(item.url)"
              >
                <span class="news-title">{{ item.title }}</span>
                <i
                  v-if="isCircularLink(item.url)"
                  class="pi pi-arrow-right news-arrow"
                  :class="{ 'pi-spin pi-spinner': resolving === extractCircularUrl(item.url) }"
                />
              </a>
            </li>
          </ul>
        </section>
      </ScrollPanel>
    </Sidebar>
  </div>
</template>

<style scoped>
.sbp-news-trigger {
  display: inline-flex;
}

:deep(.news-sidebar-root) {
  width: 28rem;
  max-width: 90vw;
}

:deep(.news-sidebar-header) {
  font-weight: 600;
  font-size: 1.1rem;
}

:deep(.news-sidebar-content) {
  padding: 0;
}

.news-scroll {
  height: calc(100vh - 8rem);
  padding: 0.5rem 0;
}

.news-section {
  padding: 0.75rem 1.25rem;
}

.news-section + .news-section {
  border-top: 1px solid var(--p-content-border-color, #e5e7eb);
  margin-top: 0;
}

.news-section-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin: 0 0 0.75rem;
  font-size: 0.95rem;
  font-weight: 600;
}

.news-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.news-item {
  display: flex;
  align-items: flex-start;
  gap: 0.5rem;
  padding: 0.5rem 0;
}

.news-item + .news-item {
  border-top: 1px solid var(--p-content-border-color, #e5e7eb);
}

.news-item-icon {
  margin-top: 0.2rem;
  font-size: 0.75rem;
  color: var(--p-text-muted-color, #6b7280);
  flex-shrink: 0;
}

.news-link {
  display: flex;
  align-items: flex-start;
  gap: 0.35rem;
  text-decoration: none;
  color: var(--p-text-color, #1f2937);
  font-size: 0.875rem;
  line-height: 1.4;
  cursor: pointer;
  flex: 1;
  min-width: 0;
}

.news-link:hover {
  color: var(--p-primary-color, #3b82f6);
}

.news-link.circular-link {
  color: var(--p-primary-color, #3b82f6);
  font-weight: 500;
}

.news-link.circular-link:hover {
  text-decoration: underline;
}

.news-title {
  flex: 1;
  min-width: 0;
  word-wrap: break-word;
}

.news-arrow {
  flex-shrink: 0;
  font-size: 0.75rem;
  margin-top: 0.2rem;
}
</style>
