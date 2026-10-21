import {expect, test} from 'bun:test';
import {IntentAction, IntentStep} from '@vibesys/proto';
import {INTENT_REPLAYS, intentReplay} from '@vibesys/run-fixtures';

for (const kind of INTENT_REPLAYS) {
  for (const action of [
    IntentAction.PAUSE,
    IntentAction.RESUME,
    IntentAction.STOP,
    IntentAction.STEER,
  ]) {
    test(`intent replay ${kind}/${action} correlates admission and outcome`, () => {
      for (let seed = 1; seed <= 16; seed += 1) {
        const requestId = `request-${seed}`;
        const {admission, records} = intentReplay(kind, requestId, BigInt(seed), action);
        if (kind === 'park-refused') {
          expect(admission.result.case).toBe('configurationFailed');
          expect(records).toHaveLength(0);
        } else {
          expect(admission.result).toMatchObject({case: 'postIntent', value: {requestId}});
          expect(records).toHaveLength(kind === 'pending' ? 0 : 1);
          for (const record of records) {
            expect(record.seq).toBe(BigInt(seed));
            expect(record.body).toMatchObject({
              case: 'intent',
              value: {
                controlId: requestId,
                action,
                step: kind === 'applied' ? IntentStep.APPLIED : IntentStep.REFUSED,
              },
            });
          }
        }
      }
    });
  }
}
