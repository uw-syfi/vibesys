/** Immutable values; reads and writes own their bytes. Missing entries return undefined. */
export interface SkeletonCache {
  getBlob(sha256: string): Promise<Uint8Array | undefined>;
  putBlob(sha256: string, data: Uint8Array): Promise<void>;
  getKeyframe(runId: string, seq: bigint): Promise<Uint8Array | undefined>;
  putKeyframe(runId: string, seq: bigint, data: Uint8Array): Promise<void>;
  close(): void;
}
