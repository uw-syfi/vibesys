/** Replays of durable admission, journal outcomes, and a refusal before posting. */
import {create} from '@bufbuild/protobuf';
import {HostCallResponseSchema, IntentAction, IntentStep, RecordSchema} from '@vibesys/proto';

export const INTENT_REPLAYS = ['pending', 'applied', 'refused', 'park-refused'] as const;
export type IntentReplay = (typeof INTENT_REPLAYS)[number];

/** `requestId` correlates a journey's own request; sequence belongs to its replayed run. */
export function intentReplay(
  kind: IntentReplay,
  requestId = 'request-a',
  sequence = 100n,
  action = IntentAction.PAUSE,
) {
  const admission = create(HostCallResponseSchema, {
    result:
      kind === 'park-refused'
        ? {
            case: 'configurationFailed',
            value: {
              code: 'invalid_input',
              stage: 'post_intent',
              exitCode: 2,
              message: "park is not supported by this checkout's control inbox",
            },
          }
        : {case: 'postIntent', value: {requestId, orderedReader: true}},
  });
  const records =
    kind === 'applied' || kind === 'refused'
      ? [
          create(RecordSchema, {
            seq: sequence,
            epoch: 1,
            at: {seconds: 1700000100n},
            body: {
              case: 'intent',
              value: {
                controlId: requestId,
                action,
                step: kind === 'applied' ? IntentStep.APPLIED : IntentStep.REFUSED,
                ...(kind === 'refused' ? {reason: 'Run ended'} : {}),
              },
            },
          }),
        ]
      : [];
  return {admission, records};
}
