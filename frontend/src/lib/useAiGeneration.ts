import { onBeforeUnmount, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import { getAIGenerationJob, type AIGenerationJob, type ApiError } from '@/lib/api'

/**
 * Drive a background AI generation job: start it, poll it, report it.
 *
 * The circular pane and the laws reader run the same job table through the same poll
 * endpoint and differ only in which route starts the work, so the loop is a composable
 * rather than two copies. Getting it wrong twice is the failure this avoids: the epoch
 * guard below exists because navigating between documents mid-job used to leave the old
 * poll running and write its result onto the newly opened one.
 */
export function useAiGeneration(options: {
  /** Queue the job. Rejects with an ApiError; a 409 carries the job already running. */
  start: (feature: string) => Promise<AIGenerationJob>
  /** Reload the subject once the job succeeds. */
  refresh: () => Promise<void>
  /** Noun for the toast, e.g. "circular" or "document". */
  subject?: string
}) {
  const toast = useToast()
  const activeJob = ref<AIGenerationJob | null>(null)
  const subject = options.subject ?? 'document'

  let pollTimer: ReturnType<typeof setTimeout> | null = null
  let pollEpoch = 0

  /** Invalidate any in-flight poll. Called on navigation as well as unmount. */
  function stop() {
    pollEpoch += 1
    if (pollTimer) clearTimeout(pollTimer)
    pollTimer = null
    activeJob.value = null
  }

  async function poll(jobId: string, epoch: number) {
    if (epoch !== pollEpoch) return
    try {
      const job = await getAIGenerationJob(jobId)
      if (epoch !== pollEpoch) return
      activeJob.value = job

      if (job.status === 'succeeded') {
        await options.refresh()
        if (epoch !== pollEpoch) return
        activeJob.value = null
        const hasGaps = job.result_status === 'completed_with_gaps'
        toast.add({
          severity: hasGaps ? 'warn' : 'success',
          summary: hasGaps ? 'AI analysis completed with gaps' : 'AI analysis complete',
          detail: hasGaps
            ? 'Some source documents could not be analyzed.'
            : `The ${subject} analysis was updated.`,
          life: hasGaps ? 6000 : 3500,
        })
        return
      }

      if (job.status === 'failed') {
        activeJob.value = null
        toast.add({
          severity: 'error',
          summary: 'AI generation failed',
          detail: job.error || 'The background job failed.',
          life: 6000,
        })
        return
      }

      pollTimer = setTimeout(() => void poll(jobId, epoch), 1000)
    } catch (error) {
      activeJob.value = null
      toast.add({
        severity: 'error',
        summary: 'Job status unavailable',
        detail: error instanceof Error ? error.message : 'Unable to check generation status.',
        life: 5000,
      })
    }
  }

  async function generate(feature: string) {
    if (activeJob.value) return
    stop()
    const epoch = pollEpoch
    try {
      const job = await options.start(feature)
      activeJob.value = job
      void poll(job.id, epoch)
    } catch (error) {
      const apiError = error as ApiError
      // 409 means someone (or another tab) already started one; adopt it rather than
      // telling the user their click failed.
      const existing = apiError.payload?.job as AIGenerationJob | undefined
      if (apiError.status === 409 && existing?.id) {
        activeJob.value = existing
        void poll(existing.id, epoch)
        return
      }
      // A 422 is the server explaining why this document cannot be analysed at all —
      // the reason is the message, and it is the useful part.
      toast.add({
        severity: apiError.status === 422 ? 'warn' : 'error',
        summary: apiError.status === 422 ? 'Nothing to analyse' : 'Unable to start generation',
        detail: error instanceof Error ? error.message : 'The request failed.',
        life: 6000,
      })
    }
  }

  onBeforeUnmount(stop)

  return { activeJob, generate, stop }
}
