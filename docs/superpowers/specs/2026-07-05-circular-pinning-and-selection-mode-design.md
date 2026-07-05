# Circular pinning relocation + sidebar selection-mode toggle

**Date:** 2026-07-05
**Scope:** `frontend/src/views/CircularsView.vue`, `frontend/src/components/CircularDetailPane.vue`

## Problem

Two UX issues in the circulars search/browse screen:

1. Pinning a circular to the active research workspace is only possible from the
   search sidebar's result rows. It should instead be an action in the circular
   detail view.
2. The sidebar shows a selection checkbox on every result row at all times, even
   though selection is only needed occasionally (for ZIP download / Chat handoff /
   workspace creation). It should be hidden by default and revealed via an explicit
   UI toggle.

## Design

### 1. Move pinning to the detail view

- **Plain search results** (`CircularsView.vue`, the `searchRows` loop, currently
  ~line 647-673): remove the pin/unpin `Button` (currently ~line 657-666). Only the
  selection `Checkbox` remains in `.result-select`.
- **Pinned section** (`CircularsView.vue`, the `pinnedCirculars` loop, currently
  ~line 616-645): unchanged — keeps its inline unpin bookmark button so users can
  still bulk-unpin without opening each circular.
- **`CircularDetailPane.vue`**: add a pin/unpin icon button to the header actions
  row (alongside the existing generate/PDF/checklist/external-link/refresh/graph/
  chat buttons, ~line 469-518). The pane stays presentation-only:
  - New props: `isPinned: boolean`, `pinPending: boolean`.
  - New emit: `toggle-pin` (no payload; parent already has `props.id` in scope).
  - Icon mirrors the existing sidebar convention: `pi-bookmark` when unpinned,
    `pi-bookmark-fill` when pinned. `pinPending` drives the button's `:loading`
    state, same as other async actions in this header.
- **`CircularsView.vue`** wires it up: passes `:is-pinned="isPinned(selectedCircularId)"`
  and `:pin-pending="workspaceSaving"` to `<CircularDetailPane>`, and handles
  `@toggle-pin="togglePinned(selectedCircularId)"`. No new pinning logic — reuses
  the existing `togglePinned`/`isPinned`/`workspaceSaving` state that already lives
  in `CircularsView.vue`.
- If no workspace is active yet, `togglePinned` already auto-activates the default
  workspace before pinning (existing behavior, unchanged).

### 2. Selection-mode toggle in the sidebar

- New local state in `CircularsView.vue`: `const selectionMode = ref(false)`.
- New toggle button in the `search-utilities` bar (next to CSV/ZIP/Chat, currently
  ~line 605-609): icon `pi-check-square` (or `pi-check-square-fill` when active),
  label toggles `Select` / `Done`. Clicking it flips `selectionMode`.
- Turning selection mode **off** clears `selectedIds` (`selectedIds.value = []`), so
  a stale hidden selection can't silently drive ZIP download / Chat handoff.
- Template changes, all gated on `v-if="selectionMode"`:
  - The `.result-select` `<span>` wrapping the `Checkbox` in both the pinned-section
    loop and the plain search-results loop.
  - The "select page" `Checkbox` and the "N selected" count text in the
    `results-toolbar-actions` block (currently ~line 680-681).
- The ZIP/Chat buttons in `search-utilities` keep their existing
  `:disabled="!selectedIds.length"` guard — no change needed there, since clearing
  `selectedIds` on toggle-off already disables them.
- CSV export is unaffected (it doesn't depend on selection).

## Out of scope

- No backend/API changes — pinning already goes through
  `pinWorkspaceCircular`/`unpinWorkspaceCircular` in `lib/api.ts`, untouched.
- No change to how pinned circulars are grouped/displayed in the sidebar.
- No persistence of `selectionMode` across page loads/navigation (resets to
  `false` on mount, same as other transient UI state in this component).

## Testing

Manual verification in the browser (no existing frontend test suite for this
view): pin/unpin from the detail view reflects in the sidebar's Pinned section;
plain search rows no longer show a pin button; checkboxes are hidden until
"Select" is toggled on; toggling off clears the selection and disables ZIP/Chat.
