import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge} from '@vibesys/ipc';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {
  HOST_CALL_METHOD,
  hostIntentRequest,
  hostIntentResponse,
  INTENT_TAIL_METHOD,
  intentTailRequest,
  intentTailResponse,
} from '@vibesys/runs/intents';
import {createActor, fromCallback, fromPromise} from 'xstate';
import {intentWatchMachine, type RunIntentsEvent, runIntentsMachine} from '../../state/index.js';

/** All I/O belongs to invoked actors, which cancel their bridge calls on exit. */
export function intentsActor(input: {
  readonly bridge: GatewayBridge;
  readonly host: HostKey;
  readonly runId: string;
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
        watch: fromCallback<RunIntentsEvent>(({sendBack}) => {
          const watcher = createActor(
            intentWatchMachine.provide({
              actors: {
                read: fromCallback(({sendBack: ended}) => {
                  const call = input.bridge.stream(
                    input.host,
                    INTENT_TAIL_METHOD,
                    intentTailRequest(input.runId),
                    bytes => {
                      try {
                        const outcomes = intentTailResponse(bytes);
                        sendBack({type: 'outcomes', outcomes});
                      } catch (error) {
                        sendBack({type: 'watchFailed', error: String(error)});
                      }
                    },
                    error => {
                      sendBack({
                        type: 'watchFailed',
                        error: error ?? 'Intent outcome stream ended',
                      });
                      ended({type: 'ended'});
                    },
                  );
                  return () => call.cancel();
                }),
              },
            }),
            {clock: clockFrom(input.schedule)},
          ).start();
          return () => watcher.stop();
        }),
      },
    }),
    {input: {enabled: input.enabled}, clock: clockFrom(input.schedule)},
  );
}
