# Circular Pinning Relocation + Sidebar Selection-Mode Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the circular pin/unpin action from the search sidebar's plain result rows into the circular detail view, and hide the sidebar's selection checkboxes behind an explicit "Select" toggle.

**Architecture:** `CircularDetailPane.vue` gains a presentation-only pin button (props in, event out); `CircularsView.vue` keeps owning all pinning/workspace state and wires the new prop/emit to its existing `togglePinned`/`isPinned`/`workspaceSaving`. A new `selectionMode` ref in `CircularsView.vue` gates the existing checkbox markup with `v-if`.

**Tech Stack:** Vue 3 `<script setup>` + TypeScript, PrimeVue components (`Button`, `Checkbox`), no test framework in `frontend/` — verification is `vue-tsc` typecheck + manual browser check via the dev server.

## Global Constraints

- No backend/API changes. Reuse `pinWorkspaceCircular`/`unpinWorkspaceCircular` from `frontend/src/lib/api.ts` as-is.
- No change to how the sidebar's "Pinned" section groups/displays pinned circulars — it keeps its own inline unpin bookmark button unchanged.
- `frontend/src/views/CircularsView.vue` and `frontend/src/components/CircularDetailPane.vue` are the only files touched.
- Match existing code conventions in both files exactly (PrimeVue `Button` prop style, `pi pi-*` icon naming, `@click` handler naming, computed/ref naming).
- Verify each task with `cd frontend && npx vue-tsc --noEmit` (must show zero new errors) plus a manual check in the running dev server (`npm run dev`).

---

### Task 1: Add pin/unpin button to `CircularDetailPane.vue`

**Files:**
- Modify: `frontend/src/components/CircularDetailPane.vue:37-38` (props/emits)
- Modify: `frontend/src/components/CircularDetailPane.vue:469-518` (header actions template)

**Interfaces:**
- Consumes: nothing new from other tasks.
- Produces: `CircularDetailPane` now accepts props `isPinned: boolean` and `pinPending: boolean`, and emits `toggle-pin` (no payload). Task 2 wires these from the parent.

- [ ] **Step 1: Add the new props and emit**

In `frontend/src/components/CircularDetailPane.vue`, change:

```ts
const props = defineProps<{ id: string }>()
const emit = defineEmits<{ close: [] }>()
```

to:

```ts
const props = defineProps<{ id: string, isPinned: boolean, pinPending: boolean }>()
const emit = defineEmits<{ close: [], 'toggle-pin': [] }>()
```

- [ ] **Step 2: Add the pin button to the header actions row**

In the same file, find the `.detail-actions` block (starts around line 469 with the `pi-sparkles` generate button). Add a new button immediately after the closing `</Button>` of the "Generate AI analysis" button (the one with `aria-label="Generate AI analysis"`) and before the `v-if="isPdf"` PDF preview button:

```html
            <Button
              :icon="isPinned ? 'pi pi-bookmark-fill' : 'pi pi-bookmark'"
              text
              rounded
              severity="secondary"
              :loading="pinPending"
              :aria-label="isPinned ? 'Unpin circular' : 'Pin circular'"
              :title="isPinned ? 'Unpin circular' : 'Pin circular'"
              @click="emit('toggle-pin')"
            />
```

- [ ] **Step 3: Typecheck**

Run: `cd frontend && npx vue-tsc --noEmit`
Expected: no new errors. (There will be one expected error at this point: `CircularsView.vue` doesn't yet pass `isPinned`/`pinPending` to `<CircularDetailPane>` — Vue's prop typing only warns at template-compile time for missing required props in strict mode, so this may or may not surface here; if it does, that's expected and gets resolved in Task 2. Confirm no *other* new errors appear.)

- [ ] **Step 4: Commit**

```bash
cd /home/saad/Work/SBPEye
git add frontend/src/components/CircularDetailPane.vue
git commit -m "Add pin/unpin button to circular detail view header"
```

---

### Task 2: Wire pinning through `CircularsView.vue` and remove the plain-row pin button

**Files:**
- Modify: `frontend/src/views/CircularsView.vue:647-673` (plain search-results loop template)
- Modify: `frontend/src/views/CircularsView.vue:691` (`<CircularDetailPane>` usage)

**Interfaces:**
- Consumes: `CircularDetailPane`'s new `isPinned`/`pinPending` props and `toggle-pin` emit from Task 1.
- Produces: nothing new for later tasks (Task 3 is independent, in the same file but a different region).

- [ ] **Step 1: Remove the pin `Button` from the plain search-results row**

In `frontend/src/views/CircularsView.vue`, inside the `v-for="row in searchRows"` loop, change:

```html
            <span class="result-select" @click.stop>
              <Checkbox v-model="selectedIds" :value="row.id" :input-id="`select-${row.id}`" :aria-label="`Select ${row.title}`" />
              <Button
                :icon="isPinned(row.id) ? 'pi pi-bookmark-fill' : 'pi pi-bookmark'"
                text
                rounded
                size="small"
                severity="secondary"
                :disabled="workspaceSaving"
                :aria-label="isPinned(row.id) ? `Unpin ${row.title}` : `Pin ${row.title}`"
                @click="togglePinned(row.id)"
              />
            </span>
```

to:

```html
            <span class="result-select" @click.stop>
              <Checkbox v-model="selectedIds" :value="row.id" :input-id="`select-${row.id}`" :aria-label="`Select ${row.title}`" />
            </span>
```

(Leave the pinned-section loop above it — the one rendering `pinnedCirculars` with the `pi-bookmark-fill` unpin button — completely untouched.)

- [ ] **Step 2: Pass pin props and listen for the toggle-pin emit on `CircularDetailPane`**

In the same file, change:

```html
      <CircularDetailPane v-if="selectedCircularId" :id="selectedCircularId" @close="closeCircular" />
```

to:

```html
      <CircularDetailPane
        v-if="selectedCircularId"
        :id="selectedCircularId"
        :is-pinned="isPinned(selectedCircularId)"
        :pin-pending="workspaceSaving"
        @close="closeCircular"
        @toggle-pin="togglePinned(selectedCircularId)"
      />
```

- [ ] **Step 3: Typecheck**

Run: `cd frontend && npx vue-tsc --noEmit`
Expected: no errors (this resolves any prop-typing warning left over from Task 1).

- [ ] **Step 4: Manual verification in the browser**

Run: `npm run dev` (from `frontend/`), open the Circulars view.
- Search for circulars; confirm plain result rows show only a checkbox, no bookmark icon.
- Open a circular's detail view; confirm a bookmark icon button now appears in the header actions row.
- Click it; confirm the circular now appears in the sidebar's "Pinned" section, and the detail-view button switches to the filled/"Unpin" state.
- Click the sidebar's inline unpin button in the Pinned section; confirm the circular moves back to plain results and the detail-view button reverts to the hollow/"Pin" state (if that circular is still open).

- [ ] **Step 5: Commit**

```bash
cd /home/saad/Work/SBPEye
git add frontend/src/views/CircularsView.vue
git commit -m "Move circular pin action from search results row to detail view"
```

---

### Task 3: Add selection-mode toggle to hide/show sidebar checkboxes

**Files:**
- Modify: `frontend/src/views/CircularsView.vue` (script: new ref; template: toolbar button + `v-if` gates)

**Interfaces:**
- Consumes: nothing from Tasks 1-2.
- Produces: nothing consumed elsewhere (terminal task).

- [ ] **Step 1: Add the `selectionMode` ref and a helper to clear selection on toggle-off**

In `frontend/src/views/CircularsView.vue`, near the other refs (after `const selectedIds = ref<string[]>([])` at line 42), add:

```ts
const selectionMode = ref(false)
```

Near `togglePageSelection` (around line 400), add a new function:

```ts
function toggleSelectionMode() {
  selectionMode.value = !selectionMode.value
  if (!selectionMode.value) selectedIds.value = []
}
```

- [ ] **Step 2: Add the toggle button to the `search-utilities` bar**

Change:

```html
      <div class="search-utilities">
        <Button label="CSV" icon="pi pi-download" size="small" text title="Export CSV" :loading="exportLoading" @click="exportCsv" />
        <Button label="ZIP" icon="pi pi-file-zip" size="small" text title="Download selected as ZIP" :disabled="!selectedIds.length" :loading="zipLoading" @click="downloadSelectedZip" />
        <Button label="Chat" icon="pi pi-comments" size="small" text title="Open selected in Chat" :disabled="!selectedIds.length" @click="handoffToChat" />
      </div>
```

to:

```html
      <div class="search-utilities">
        <Button
          :label="selectionMode ? 'Done' : 'Select'"
          icon="pi pi-check-square"
          size="small"
          text
          :title="selectionMode ? 'Exit selection mode' : 'Select circulars'"
          @click="toggleSelectionMode"
        />
        <Button label="CSV" icon="pi pi-download" size="small" text title="Export CSV" :loading="exportLoading" @click="exportCsv" />
        <Button label="ZIP" icon="pi pi-file-zip" size="small" text title="Download selected as ZIP" :disabled="!selectedIds.length" :loading="zipLoading" @click="downloadSelectedZip" />
        <Button label="Chat" icon="pi pi-comments" size="small" text title="Open selected in Chat" :disabled="!selectedIds.length" @click="handoffToChat" />
      </div>
```

- [ ] **Step 3: Gate the pinned-section checkbox on `selectionMode`**

In the `pinnedCirculars` loop, change:

```html
              <span class="result-select" @click.stop>
                <Checkbox v-model="selectedIds" :value="circular.id" :input-id="`select-pinned-${circular.id}`" :aria-label="`Select ${circular.title}`" />
                <Button
                  icon="pi pi-bookmark-fill"
```

to:

```html
              <span class="result-select" @click.stop>
                <Checkbox v-if="selectionMode" v-model="selectedIds" :value="circular.id" :input-id="`select-pinned-${circular.id}`" :aria-label="`Select ${circular.title}`" />
                <Button
                  icon="pi pi-bookmark-fill"
```

- [ ] **Step 4: Gate the plain-row checkbox on `selectionMode`**

From Task 2's Step 1, the plain-row span is now:

```html
            <span class="result-select" @click.stop>
              <Checkbox v-model="selectedIds" :value="row.id" :input-id="`select-${row.id}`" :aria-label="`Select ${row.title}`" />
            </span>
```

Change it to:

```html
            <span v-if="selectionMode" class="result-select" @click.stop>
              <Checkbox v-model="selectedIds" :value="row.id" :input-id="`select-${row.id}`" :aria-label="`Select ${row.title}`" />
            </span>
```

(This span uses `v-if` on the wrapping `<span>` itself, unlike the pinned-section span in Step 3, because the plain row's span now contains nothing else worth showing when selection mode is off — no pin button remains there after Task 2.)

- [ ] **Step 5: Gate the "select page" checkbox and "N selected" count in the results toolbar**

Change:

```html
          <div class="results-toolbar-actions">
            <Checkbox :model-value="allPageSelected" binary aria-label="Select page" :disabled="!searchRows.length" @update:model-value="togglePageSelection" />
            <span v-if="selectedIds.length">{{ selectedIds.length }} selected</span>
```

to:

```html
          <div class="results-toolbar-actions">
            <Checkbox v-if="selectionMode" :model-value="allPageSelected" binary aria-label="Select page" :disabled="!searchRows.length" @update:model-value="togglePageSelection" />
            <span v-if="selectionMode && selectedIds.length">{{ selectedIds.length }} selected</span>
```

- [ ] **Step 6: Typecheck**

Run: `cd frontend && npx vue-tsc --noEmit`
Expected: no errors.

- [ ] **Step 7: Manual verification in the browser**

Run: `npm run dev` (from `frontend/`), open the Circulars view.
- Confirm no checkboxes are visible anywhere in the results list or toolbar by default.
- Click "Select" in the utilities bar; confirm checkboxes appear on every row (pinned and plain) and the "select page" checkbox + count appear in the toolbar; button now reads "Done".
- Select a couple of rows; confirm ZIP/Chat buttons become enabled and the count updates.
- Click "Done"; confirm checkboxes disappear, ZIP/Chat buttons go back to disabled, and the toolbar count/select-page checkbox disappear.

- [ ] **Step 8: Commit**

```bash
cd /home/saad/Work/SBPEye
git add frontend/src/views/CircularsView.vue
git commit -m "Hide sidebar selection checkboxes behind a Select toggle"
```
