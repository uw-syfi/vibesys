/**
 * Pacing for the cold upload of the gateway over ssh. Bursts can reset ssh jump connections, so
 * the artifact goes in fixed chunks, each after a wait that keeps the average rate at or below
 * the cap.
 */

export const UPLOAD_BYTES_PER_SECOND = 512 * 1024;
export const UPLOAD_CHUNK_BYTES = 32 * 1024;

export interface UploadStep {
  /** The wait before this chunk is written. */
  readonly delayMs: number;
  /** Where the chunk starts, and how long it is. */
  readonly offset: number;
  readonly length: number;
}

/** The chunks of `total` bytes in order, with the wait before each. */
export function uploadSchedule(total: number): readonly UploadStep[] {
  const steps: UploadStep[] = [];
  for (let offset = 0; offset < total; offset += UPLOAD_CHUNK_BYTES) {
    const length = Math.min(UPLOAD_CHUNK_BYTES, total - offset);
    // Node truncates fractional timer delays; round up to preserve the rate cap.
    steps.push({delayMs: Math.ceil((1000 * length) / UPLOAD_BYTES_PER_SECOND), offset, length});
  }
  return steps;
}
