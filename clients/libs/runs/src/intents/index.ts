/** The protobuf boundary for run intents. UI code consumes values and opaque bytes. */
import {clone, create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  type ConfigurationFailed,
  HostCallRequestSchema,
  HostCallResponseSchema,
  HostService,
  type IntentState,
  type PostIntentRequest,
  PostIntentRequestSchema,
  type PostIntentResponse,
  RecordSchema,
  ResumeWhen,
  SteerMode,
  SteerTargetKind,
  StopMode,
  TailRequestSchema,
  TailResponseSchema,
} from '@vibesys/proto';

export type {IntentState, PostIntentRequest, PostIntentResponse} from '@vibesys/proto';
export {IntentStep} from '@vibesys/proto';

export const HOST_CALL_METHOD = `/${HostService.typeName}/${HostService.method.hostCall.name}`;
export const INTENT_TAIL_METHOD = `/${HostService.typeName}/${HostService.method.tail.name}`;

/** The visible controls' defaults. The host surface accepts every typed intent. */
export function runIntent(
  identity: Pick<PostIntentRequest, 'projectRoot' | 'runId' | 'requestId'>,
  action: 'pause' | 'park' | 'resume' | 'stop' | 'steer',
  text = '',
): PostIntentRequest {
  switch (action) {
    case 'pause':
    case 'park':
      return create(PostIntentRequestSchema, {...identity, intent: {case: action, value: {}}});
    case 'resume':
      return create(PostIntentRequestSchema, {
        ...identity,
        intent: {case: 'resume', value: {when: ResumeWhen.NOW}},
      });
    case 'stop':
      return create(PostIntentRequestSchema, {
        ...identity,
        intent: {case: 'stop', value: {mode: StopMode.DRAIN}},
      });
    case 'steer':
      return create(PostIntentRequestSchema, {
        ...identity,
        intent: {
          case: 'steer',
          value: {text, mode: SteerMode.NEXT_TURN, target: {kind: SteerTargetKind.RUN}},
        },
      });
  }
}

export type RunIntentAction = Parameters<typeof runIntent>[1];

/** Take ownership before a mutable generated request enters control state. */
export function copyIntent(request: PostIntentRequest): PostIntentRequest {
  return clone(PostIntentRequestSchema, request);
}

export function hostIntentRequest(request: PostIntentRequest): Uint8Array {
  return toBinary(
    HostCallRequestSchema,
    create(HostCallRequestSchema, {method: {case: 'postIntent', value: request}}),
  );
}

/** The host's saved checkout is authoritative, even if renderer bytes name another. */
export function configuredHostCall(bytes: Uint8Array, checkout: string | null): Uint8Array {
  if (checkout === null) throw new Error('Set a checkout first');
  const request = fromBinary(HostCallRequestSchema, bytes);
  request.checkout = checkout;
  return toBinary(HostCallRequestSchema, request);
}

/** A typed negative HostCall reply, distinct from an uncertain transport failure. */
export class IntentRefused extends Error {
  constructor(failure: ConfigurationFailed) {
    super(`${failure.message}${failure.stderr ? `: ${failure.stderr}` : ''}`);
    this.name = 'IntentRefused';
  }
}

export function hostIntentResponse(bytes: Uint8Array, requestId: string): PostIntentResponse {
  const response = fromBinary(HostCallResponseSchema, bytes);
  if (response.result.case === 'configurationFailed') {
    const failure = response.result.value;
    if (
      ['admission_unconfirmed', 'host_command_failed', 'invalid_host_response'].includes(
        failure.code,
      )
    )
      throw new Error(
        `${failure.message} Admission is not confirmed; retry uses the same request id.`,
      );
    throw new IntentRefused(failure);
  }
  if (response.result.case !== 'postIntent' || response.result.value.requestId !== requestId)
    throw new Error('HostCall returned an invalid admission reply');
  return response.result.value;
}

export function intentTailRequest(runId: string): Uint8Array {
  return toBinary(TailRequestSchema, create(TailRequestSchema, {runId}));
}

export function intentTailResponse(bytes: Uint8Array): readonly IntentState[] {
  return fromBinary(TailResponseSchema, bytes).records.flatMap(bytes => {
    const record = fromBinary(RecordSchema, bytes);
    return record.body.case === 'intent' ? [record.body.value] : [];
  });
}

export {
  copyRelaunch,
  copyRelaunchObservation,
  hostRelaunchRequest,
  hostRelaunchResponse,
  livenessRequest,
  livenessResponse,
  type RelaunchLiveness,
  type RelaunchObservation,
  RelaunchRefusal,
  type RelaunchRequest,
  type RelaunchResponse,
  type RelaunchTarget,
  RUN_LIVENESS_METHOD,
  RunLiveness,
  relaunchLabel,
  relaunchRequest,
  relaunchTarget,
} from './relaunch.js';
