import { onBeforeUnmount, ref } from 'vue'

interface ResizablePaneOptions {
  /** When true, dragging right shrinks the pane instead of growing it (for panes anchored on the right edge). */
  reverse?: boolean
}

export function useResizablePane(
  storageKey: string,
  defaultPx: number,
  minPx: number,
  maxPx: number,
  options: ResizablePaneOptions = {},
) {
  const stored = Number(localStorage.getItem(storageKey))
  const size = ref(Number.isFinite(stored) && stored > 0 ? clamp(stored) : defaultPx)
  const resizing = ref(false)

  let startX = 0
  let startSize = 0

  function clamp(value: number): number {
    return Math.min(maxPx, Math.max(minPx, value))
  }

  function onPointerMove(event: PointerEvent) {
    const delta = event.clientX - startX
    size.value = clamp(startSize + (options.reverse ? -delta : delta))
  }

  function stopDrag() {
    if (!resizing.value) return
    resizing.value = false
    document.removeEventListener('pointermove', onPointerMove)
    document.removeEventListener('pointerup', stopDrag)
    document.body.style.removeProperty('cursor')
    document.body.style.removeProperty('user-select')
    localStorage.setItem(storageKey, String(size.value))
  }

  function startDrag(event: PointerEvent) {
    resizing.value = true
    startX = event.clientX
    startSize = size.value
    document.addEventListener('pointermove', onPointerMove)
    document.addEventListener('pointerup', stopDrag)
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    event.preventDefault()
  }

  function resetToDefault() {
    size.value = defaultPx
    localStorage.removeItem(storageKey)
  }

  onBeforeUnmount(stopDrag)

  return { size, resizing, startDrag, resetToDefault }
}
