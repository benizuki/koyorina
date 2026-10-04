import type { GenerationJob } from '@/types'

export const generationStamp = (job: GenerationJob) =>
  `第${job.revision}版 ／ ${new Date(job.created_at).toLocaleString('ja-JP')}`
