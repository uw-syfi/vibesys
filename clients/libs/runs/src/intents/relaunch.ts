/** Relaunch uses HostCall; the epoch is confirmed by the host writer. */
import {clone, create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  HostCallRequestSchema,
  HostCallResponseSchema,
  RelaunchRefusal,
  type RelaunchRequest,
  RelaunchRequestSchema,
  type RelaunchResponse,
} from '@vibesys/proto';

export type {RelaunchRequest, RelaunchResponse} from '@vibesys/proto';
export {RelaunchRefusal} from '@vibesys/proto';

/** Only these reader observations offer relaunch; the host rechecks admission. */
export interface RelaunchTarget {
  readonly epoch: bigint;
  readonly state: 'parked' | 'stopped-resumable' | 'interrupted';
}

export function relaunchRequest(
  projectRoot: string,
  runId: string,
  expectedEpoch: bigint,
): RelaunchRequest {
  return create(RelaunchRequestSchema, {projectRoot, runId, expectedEpoch});
}

export function copyRelaunch(request: RelaunchRequest): RelaunchRequest {
  return clone(RelaunchRequestSchema, request);
}

export function hostRelaunchRequest(request: RelaunchRequest): Uint8Array {
  return toBinary(
    HostCallRequestSchema,
    create(HostCallRequestSchema, {method: {case: 'relaunch', value: request}}),
  );
}

export function hostRelaunchResponse(bytes: Uint8Array, expectedEpoch: bigint): RelaunchResponse {
  const response = fromBinary(HostCallResponseSchema, bytes);
  if (response.result.case === 'configurationFailed')
    throw new Error(response.result.value.message);
  if (response.result.case !== 'relaunch') throw new Error('Invalid relaunch response');
  const result = response.result.value;
  if (
    (result.outcome.case === 'epoch' && result.outcome.value > expectedEpoch) ||
    (result.outcome.case === 'refusal' &&
      [
        RelaunchRefusal.LOCKED,
        RelaunchRefusal.ENDED,
        RelaunchRefusal.CHECKOUT_MISSING,
        RelaunchRefusal.EPOCH_CHANGED,
      ].includes(result.outcome.value))
  )
    return result;
  throw new Error('Invalid relaunch outcome');
}

export function relaunchLabel(result: RelaunchResponse | null): string | null {
  if (!result) return null;
  if (result.outcome.case === 'epoch') return 'Epoch ' + result.outcome.value.toString();
  switch (result.outcome.value) {
    case RelaunchRefusal.LOCKED:
      return 'Refused: locked';
    case RelaunchRefusal.ENDED:
      return 'Refused: ended';
    case RelaunchRefusal.CHECKOUT_MISSING:
      return 'Refused: checkout missing';
    case RelaunchRefusal.EPOCH_CHANGED:
      return 'Refused: epoch changed';
    default:
      return null;
  }
}
