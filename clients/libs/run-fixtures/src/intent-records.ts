/** Replays of durable admission, journal outcomes, and park outcomes. */
import {create} from '@bufbuild/protobuf';
import {HostCallResponseSchema, IntentAction, IntentStep, RecordSchema} from '@vibesys/proto';

export const INTENT_REPLAYS = ['pending', 'applied', 'refused', 'park-applied'] as const;
export type IntentReplay = (typeof INTENT_REPLAYS)[number];

/** `requestId` correlates a journey's own request; sequence belongs to its replayed run. */
export function intentReplay(
  kind: IntentReplay,
  requestId = 'request-a',
  sequence = 100n,
  action = IntentAction.PAUSE,
) {
  const admission = create(HostCallResponseSchema, {
    result: {case: 'postIntent', value: {requestId, orderedReader: true}},
  });
  const records =
    kind !== 'pending'
      ? [
          create(RecordSchema, {
            seq: sequence,
            epoch: 1,
            at: {seconds: 1700000100n},
            body: {
              case: 'intent',
              value: {
                controlId: requestId,
                action: kind === 'park-applied' ? IntentAction.PARK : action,
                step: kind === 'refused' ? IntentStep.REFUSED : IntentStep.APPLIED,
                ...(kind === 'refused' ? {reason: 'Run ended'} : {}),
              },
            },
          }),
        ]
      : [];
  return {admission, records};
}
