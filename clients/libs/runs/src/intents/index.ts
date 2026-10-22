/** The protobuf boundary for run intents. UI code consumes values and opaque bytes. */
import {clone, create, fromBinary, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
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
  SteerMode,
  SteerTargetKind,
  TailRequestSchema,
  TailResponseSchema,
  TerminateRunOutcome,
} from '@vibesys/proto';

export type {IntentState, PostIntentRequest, PostIntentResponse} from '@vibesys/proto';
export {IntentStep} from '@vibesys/proto';

export const HOST_CALL_METHOD = `/${HostService.typeName}/${HostService.method.hostCall.name}`;
export const INTENT_TAIL_METHOD = `/${HostService.typeName}/${HostService.method.tail.name}`;

const referenceBrand: unique symbol = Symbol('RunReference');

/** An addressed run; only the validating constructor can supply the project path. */
export interface RunReference {
  readonly [referenceBrand]: true;
  readonly projectRoot: string;
  readonly runId: string;
}

export function runReference(projectRoot: string, runId: string): RunReference {
  if (projectRoot.trim() === '') throw new Error('run.projectRoot is required');
  if (runId.trim() === '') throw new Error('run.runId is required');
  return {[referenceBrand]: true, projectRoot, runId};
}

export function addressedIntent(
  reference: RunReference,
  requestId: string,
  expectedEpoch: bigint,
  intent: NonNullable<MessageInitShape<typeof PostIntentRequestSchema>['intent']>,
): PostIntentRequest {
  return create(PostIntentRequestSchema, {
    projectRoot: reference.projectRoot,
    runId: reference.runId,
    requestId,
    expectedEpoch,
    intent,
  });
}

/** The visible controls' defaults. The host surface accepts every typed intent. */
export function runIntent(
  identity: RunReference & Pick<PostIntentRequest, 'requestId' | 'expectedEpoch'>,
  action: 'pause' | 'pauseNow' | 'steer',
  text = '',
): PostIntentRequest {
  switch (action) {
    case 'pause':
    case 'pauseNow':
      return addressedIntent(identity, identity.requestId, identity.expectedEpoch, {
        case: action,
        value: {},
      });
    case 'steer':
      return addressedIntent(identity, identity.requestId, identity.expectedEpoch, {
        case: 'steer',
        value: {text, mode: SteerMode.NEXT_TURN, target: {kind: SteerTargetKind.RUN}},
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
  intentAdmissionWarning(response.result.value);
  return response.result.value;
}

/** Admission and process observation never claim that a control was applied. */
export function intentAdmissionWarning(response: PostIntentResponse): string | null {
  const order = response.orderedReader
    ? null
    : 'Posting order has not been confirmed. Start or restart this run with the current VibeSys.';
  const termination = response.termination;
  if (!termination) return order;
  if (termination.requestId !== response.requestId)
    throw new Error('Termination identity mismatch');
  let observation: string;
  switch (termination.outcome) {
    case TerminateRunOutcome.WRITER_RELEASED:
      observation = 'Pause now recorded. Waiting for the run to publish its status.';
      break;
    case TerminateRunOutcome.IDENTITY_CHANGED:
      observation = 'The run process changed. Pause now was not escalated.';
      break;
    case TerminateRunOutcome.UNSUPPORTED:
      observation = 'Pause now recorded. Safe process termination is unsupported on this host.';
      break;
    case TerminateRunOutcome.REQUESTED_UNCONFIRMED:
      observation = 'Termination requested. The run process has not confirmed its exit.';
      break;
    case TerminateRunOutcome.OBSERVATION_UNKNOWN:
      observation = 'Pause now recorded. The run process identity could not be confirmed.';
      break;
    case TerminateRunOutcome.PROCESS_ABSENT:
      observation = 'The run process was absent. Waiting for the run to publish its status.';
      break;
    default:
      throw new Error('Unknown termination outcome');
  }
  return [order, observation].filter(value => value !== null).join(' ');
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
