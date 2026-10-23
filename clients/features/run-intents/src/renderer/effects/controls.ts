import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge} from '@vibesys/ipc';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {
  HOST_CALL_METHOD,
  hostIntentRequest,
  hostIntentResponse,
  intentOutcomes,
} from '@vibesys/runs/intents';
import {createActor, fromCallback, fromPromise} from 'xstate';
import {type RunIntentsEvent, runIntentsMachine} from '../../state/index.js';

/** Subscribes to the records the run's one reader delivers; returns the unsubscribe. */
export type RunRecords = (listener: (records: readonly Uint8Array[]) => void) => () => void;

/** All I/O belongs to invoked actors, which cancel their bridge calls on exit. */
export function intentsActor(input: {
  readonly bridge: GatewayBridge;
  readonly host: HostKey;
  readonly runId: string;
  readonly records: RunRecords;
  readonly enabled: boolean;
  readonly schedule: ScheduleTimeout;
}) {
  return createActor(
    runIntentsMachine.provide({
      actors: {
        post: fromPromise(async ({input: request, signal}) => {
          const call = input.bridge.unary(input.host, HOST_CALL_METHOD, hostIntentRequest(request));
          const cancel = () => call.cancel();
          signal.addEventListener('abort', cancel, {once: true});
          try {
            if (signal.aborted) throw new Error('intent cancelled');
            return hostIntentResponse(await call.result, request.requestId);
          } finally {
            signal.removeEventListener('abort', cancel);
            call.cancel();
          }
        }),
        watch: fromCallback<RunIntentsEvent>(({sendBack}) =>
          input.records(records => {
            const outcomes = intentOutcomes(records);
            if (outcomes.length > 0) sendBack({type: 'outcomes', outcomes});
          }),
        ),
      },
    }),
    {input: {enabled: input.enabled}, clock: clockFrom(input.schedule)},
  );
}
