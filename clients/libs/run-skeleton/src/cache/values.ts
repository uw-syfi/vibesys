import {fromBinary} from '@bufbuild/protobuf';
import {KeyframeSchema} from '@vibesys/proto';

export function sameBytes(left: Uint8Array, right: Uint8Array): boolean {
  return left.length === right.length && left.every((byte, index) => byte === right[index]);
}

export async function blobValue(hash: string, data: Uint8Array): Promise<Uint8Array> {
  if (!/^[0-9a-f]{64}$/.test(hash)) throw new Error('blob.sha256: expected lowercase SHA-256');
  const bytes = new Uint8Array(data);
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  const actual = Array.from(digest, byte => byte.toString(16).padStart(2, '0')).join('');
  if (actual !== hash) throw new Error('blob.sha256: content does not match hash');
  return bytes;
}

export function keyframeKey(runId: string, seq: bigint): string[] {
  if (!runId || seq <= 0n) throw new Error('keyframe: run_id and positive seq required');
  return [runId, seq.toString()];
}

export function keyframeValue(seq: bigint, data: Uint8Array): Uint8Array {
  const bytes = new Uint8Array(data);
  if (fromBinary(KeyframeSchema, bytes).throughSeq !== seq) {
    throw new Error('keyframe.through_seq: does not match cache key');
  }
  return bytes;
}
