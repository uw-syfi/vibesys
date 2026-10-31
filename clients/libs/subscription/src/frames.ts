/**
 * What the shell tells a subscription's machine about its stream, read from the gateway's frames.
 * The shell validates each frame against the generated schema before it gets here; this module
 * only names what a valid frame means, so the machine never sees a protobuf message.
 */
import {type SubscribeResponse, type TopicEntry, TopicFailureCode} from '@vibesys/proto';
import type {ConnectStep, FailureReason, Instant} from './state.js';

/**
 * What a stream reports to its machine. `at` is the shell's clock when the frame arrived: time
 * reaches the machine as data, so the machine reads no clock of its own.
 */
export type StreamEvent =
  | {readonly type: 'reached'; readonly step: ConnectStep}
  | {readonly type: 'beat'; readonly at: Instant}
  | {readonly type: 'entry'; readonly entry: TopicEntry; readonly at: Instant}
  | {readonly type: 'removed'; readonly key: string; readonly at: Instant}
  | {readonly type: 'synced'; readonly at: Instant}
  | {readonly type: 'refused'; readonly reason: FailureReason};

/** Why a topic failure ends the stream. Only an unsupported topic ends the retries. */
const REFUSALS: Readonly<
  Record<Exclude<TopicFailureCode, TopicFailureCode.UNSPECIFIED>, FailureReason>
> = {
  [TopicFailureCode.UNSUPPORTED]: 'unsupported',
  [TopicFailureCode.RESOURCE_EXHAUSTED]: 'resource_exhausted',
  [TopicFailureCode.SOURCE_FAILED]: 'source_failed',
  [TopicFailureCode.INTERNAL]: 'internal',
};

/** The event for one gateway frame. A frame that names no case, or no failure code, is invalid. */
export function streamEvent(frame: SubscribeResponse, at: Instant): StreamEvent {
  const body = frame.frame;
  switch (body.case) {
    case 'entry':
      return {type: 'entry', entry: body.value, at};
    case 'removed':
      return {type: 'removed', key: body.value, at};
    case 'synced':
      return {type: 'synced', at};
    case 'heartbeat':
      return {type: 'beat', at};
    case 'failed': {
      const code = body.value.code;
      return {
        type: 'refused',
        reason: code === TopicFailureCode.UNSPECIFIED ? 'invalid_frame' : REFUSALS[code],
      };
    }
    default:
      return {type: 'refused', reason: 'invalid_frame'};
  }
}
