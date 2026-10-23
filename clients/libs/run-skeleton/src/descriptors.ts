import {fromBinary, toBinary} from '@bufbuild/protobuf';
import {type FileDescriptorSet, FileDescriptorSetSchema} from '@bufbuild/protobuf/wkt';
import {sha256} from '@noble/hashes/sha2.js';
import {bytesToHex} from '@noble/hashes/utils.js';
import {KeyframeSchema, type SkeletonResponse} from '@vibesys/proto';

/** A keyframe names descriptors that neither the reply nor the reader's cache holds. */
export class DescriptorsUnknown extends Error {
  /** gRPC DataLoss: retrying the same request cannot supply the missing set. */
  readonly status = 15;
  constructor(
    readonly sha256: string,
    readonly epochSeq: bigint,
  ) {
    super(`keyframe.epochs[${epochSeq}].descriptors_sha256: no descriptor set ${sha256}`);
    this.name = 'DescriptorsUnknown';
  }
}

const MAX_SETS = 16;

/**
 * Descriptor sets by the SHA-256 of their bytes, held in memory. The gateway sends a set once,
 * then only names it, so a reader that kept the set never receives it again. A set is verified
 * against its name before use; an unknown name is an error, never a guess.
 */
export class DescriptorCache {
  readonly #sets = new Map<string, FileDescriptorSet>();

  /** The names to list in the next Skeleton request. */
  known(): string[] {
    return [...this.#sets.keys()];
  }

  /** The reply's keyframe with each epoch's inline descriptors restored from its hash. */
  restore(response: SkeletonResponse): SkeletonResponse {
    for (const sent of response.descriptorSets) {
      if (bytesToHex(sha256(sent.data)) !== sent.sha256)
        throw new Error(`descriptor_sets: ${sent.sha256} does not match its bytes`);
      this.#remember(sent.sha256, fromBinary(FileDescriptorSetSchema, sent.data));
    }
    if (response.keyframe === undefined) return response;
    const keyframe = fromBinary(KeyframeSchema, response.keyframe);
    let named = false;
    for (const version of keyframe.epochs) {
      if (version.descriptorsSha256 === '') continue;
      const set = this.#sets.get(version.descriptorsSha256);
      if (!set || !version.epoch)
        throw new DescriptorsUnknown(version.descriptorsSha256, version.seq);
      version.epoch.descriptors = set;
      version.descriptorsSha256 = '';
      named = true;
    }
    return named ? {...response, keyframe: toBinary(KeyframeSchema, keyframe)} : response;
  }

  #remember(sha256: string, set: FileDescriptorSet): void {
    this.#sets.delete(sha256);
    this.#sets.set(sha256, set);
    if (this.#sets.size > MAX_SETS) {
      const oldest = this.#sets.keys().next().value;
      if (oldest !== undefined) this.#sets.delete(oldest);
    }
  }
}
