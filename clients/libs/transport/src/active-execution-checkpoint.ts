import type {
  ActiveExecutionsChunkMessage,
  EventBatchMessage,
  ServerMessage,
} from '@vibesys/contracts';
import {BackendClientError, parseActiveExecutionsCheckpoint} from '@vibesys/contracts';
import {sha256Utf8, utf8ByteLength} from './sha256.js';

// Generated schema source: MAX_ACTIVE_EXECUTIONS_CHECKPOINT_BYTES in
// server.api.protocol. This runtime guard covers the aggregate that no one
// fragment's generated schema can express.
const MAX_CHECKPOINT_BYTES = 16 * 1024 * 1024;

interface StagedCheckpoint {
  readonly checkpointId: string;
  readonly chunkCount: number;
  nextIndex: number;
  byteLength: number;
  readonly payload: string[];
  readonly throughSequence: number;
  readonly storeId: string;
  readonly historyAfterSequence: number;
}

interface DeferredCheckpoint {
  readonly checkpointId: string;
  readonly throughSequence: number;
  readonly storeId: string;
  readonly historyAfterSequence: number;
}

type SemanticServerMessage = Exclude<ServerMessage, {type: 'active_executions_chunk'}>;

export interface ActiveExecutionCheckpointDisposition {
  readonly kind: 'deferred' | 'complete';
  readonly checkpointId: string;
}

const completedCheckpoints = new WeakMap<EventBatchMessage, string>();

/**
 * Describes the private fold transaction carried by an event batch.
 *
 * Deferred wire batches name themselves. Completed batches are synthetic
 * objects produced below and are branded out of band, keeping the wire model
 * closed while letting the run fold commit its staged execution projection.
 */
export function activeExecutionCheckpointDisposition(
  message: EventBatchMessage,
): ActiveExecutionCheckpointDisposition | undefined {
  if (message.active_executions === null) {
    return {kind: 'deferred', checkpointId: message.active_executions_checkpoint_id};
  }
  const checkpointId = completedCheckpoints.get(message);
  return checkpointId === undefined ? undefined : {kind: 'complete', checkpointId};
}

function checkpointFailure(message: string): BackendClientError {
  return new BackendClientError('parse', `Invalid active-execution checkpoint: ${message}`);
}

function requireDeferredContinuation(
  previous: DeferredCheckpoint,
  message: EventBatchMessage,
): void {
  if (message.rebootstrap === true) {
    throw checkpointFailure('rebootstrap marker repeated after the first deferred event frame');
  }
  if (
    message.store_id !== previous.storeId ||
    message.history_after_sequence !== previous.historyAfterSequence ||
    (message.through_sequence ?? 0) < previous.throughSequence
  ) {
    throw checkpointFailure('cursor metadata changed between deferred event frames');
  }
}

/**
 * Stages transport fragments and publishes only a complete authoritative list.
 *
 * One instance belongs to one subscription generation. An index-zero chunk
 * supersedes an interrupted transfer; every later chunk must continue the one
 * staged transfer exactly. Reliable transports preserve ordering, so a gap or
 * duplicate is a protocol fault rather than something a client can repair.
 */
export class ActiveExecutionCheckpointAssembler {
  #deferred: DeferredCheckpoint | null = null;
  #staged: StagedCheckpoint | null = null;

  reset(): void {
    this.#deferred = null;
    this.#staged = null;
  }

  accept(message: ServerMessage): SemanticServerMessage | null {
    if (message.type === 'active_executions_chunk') return this.#acceptChunk(message);
    if (message.type === 'event_batch') {
      if (message.active_executions === null) this.#observeDeferredBatch(message);
      else this.reset();
      return message;
    }
    if (this.#deferred !== null || this.#staged !== null) {
      if (message.type === 'protocol_error') {
        this.reset();
        return message;
      }
      throw checkpointFailure(`${message.type} interrupted a deferred checkpoint transfer`);
    }
    return message;
  }

  #observeDeferredBatch(message: EventBatchMessage): void {
    const checkpointId = message.active_executions_checkpoint_id;
    if (checkpointId == null) {
      throw checkpointFailure('deferred event batch has no checkpoint identity');
    }
    const deferred = this.#deferred;
    if (this.#staged !== null || (deferred !== null && checkpointId !== deferred.checkpointId)) {
      // A new deferred batch is an authoritative successor. Its advertised
      // identity fences every late fragment of the abandoned transfer.
      this.reset();
    } else if (deferred !== null) requireDeferredContinuation(deferred, message);
    this.#deferred = {
      checkpointId,
      throughSequence: message.through_sequence ?? 0,
      storeId: message.store_id ?? '',
      historyAfterSequence: message.history_after_sequence ?? 0,
    };
  }

  #acceptChunk(message: ActiveExecutionsChunkMessage): EventBatchMessage | null {
    if (message.chunk_index === 0) {
      const deferred = this.#deferred;
      if (deferred === null) {
        throw checkpointFailure('first chunk has no preceding deferred event batch');
      }
      if (this.#staged !== null) {
        throw checkpointFailure('first chunk repeated before the transfer completed');
      }
      if (
        message.checkpoint_id !== deferred.checkpointId ||
        message.through_sequence !== deferred.throughSequence ||
        message.store_id !== deferred.storeId ||
        message.history_after_sequence !== deferred.historyAfterSequence
      ) {
        throw checkpointFailure('first chunk does not match its deferred event batch');
      }
      const byteLength = utf8ByteLength(message.payload);
      if (byteLength > MAX_CHECKPOINT_BYTES) {
        throw checkpointFailure(`assembled payload exceeds ${MAX_CHECKPOINT_BYTES} UTF-8 bytes`);
      }
      this.#staged = {
        checkpointId: message.checkpoint_id,
        chunkCount: message.chunk_count,
        nextIndex: 1,
        byteLength,
        payload: [message.payload],
        throughSequence: message.through_sequence,
        storeId: message.store_id,
        historyAfterSequence: message.history_after_sequence,
      };
    } else {
      this.#append(message);
    }
    const staged = this.#staged;
    if (staged === null || staged.nextIndex !== staged.chunkCount) return null;
    this.reset();
    const payload = staged.payload.join('');
    if (sha256Utf8(payload) !== staged.checkpointId) {
      throw checkpointFailure('checkpoint_id does not match the assembled UTF-8 payload');
    }
    const activeExecutions = parseActiveExecutionsCheckpoint(payload);
    const completed: EventBatchMessage = {
      type: 'event_batch',
      // Every deferred event frame was already folded exactly once. Completion
      // publishes only the authoritative checkpoint, so the assembler retains
      // no unbounded replay history and consumers observe no duplicate effects.
      events: [],
      through_sequence: staged.throughSequence,
      active_executions: activeExecutions,
      store_id: staged.storeId,
      history_after_sequence: staged.historyAfterSequence,
      rebootstrap: false,
    };
    completedCheckpoints.set(completed, staged.checkpointId);
    return completed;
  }

  #append(message: ActiveExecutionsChunkMessage): void {
    const staged = this.#staged;
    if (staged === null) throw checkpointFailure(`chunk ${message.chunk_index} has no first chunk`);
    if (message.checkpoint_id !== staged.checkpointId) {
      throw checkpointFailure('checkpoint_id changed before the transfer completed');
    }
    if (message.chunk_count !== staged.chunkCount) {
      throw checkpointFailure('chunk_count changed before the transfer completed');
    }
    if (message.chunk_index !== staged.nextIndex) {
      throw checkpointFailure(
        `expected chunk ${staged.nextIndex}, received ${message.chunk_index}`,
      );
    }
    if (
      message.through_sequence !== staged.throughSequence ||
      message.store_id !== staged.storeId ||
      message.history_after_sequence !== staged.historyAfterSequence
    ) {
      throw checkpointFailure('cursor metadata changed before the transfer completed');
    }
    const byteLength = staged.byteLength + utf8ByteLength(message.payload);
    if (byteLength > MAX_CHECKPOINT_BYTES) {
      throw checkpointFailure(`assembled payload exceeds ${MAX_CHECKPOINT_BYTES} UTF-8 bytes`);
    }
    staged.payload.push(message.payload);
    staged.byteLength = byteLength;
    staged.nextIndex += 1;
  }
}
