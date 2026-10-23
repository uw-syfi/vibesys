import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge} from '@vibesys/ipc';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {
  HOST_CALL_METHOD,
  hostRelaunchRequest,
  hostRelaunchResponse,
  type RelaunchObservation,
  type RelaunchRequest,
} from '@vibesys/runs/intents';
import {createActor, fromPromise} from 'xstate';
import {relaunchMachine} from '../../state/index.js';

export function relaunchActor(input: {
  readonly bridge: GatewayBridge;
  readonly host: HostKey;
  readonly request: RelaunchRequest;
  readonly observation: RelaunchObservation;
  readonly schedule: ScheduleTimeout;
}) {
  return createActor(
    relaunchMachine.provide({
      actors: {
        launch: fromPromise(async ({input: request, signal}) => {
          const call = input.bridge.unary(
            input.host,
            HOST_CALL_METHOD,
            hostRelaunchRequest(request),
          );
          const cancel = () => call.cancel();
          signal.addEventListener('abort', cancel, {once: true});
          try {
            if (signal.aborted) throw new Error('relaunch cancelled');
            return hostRelaunchResponse(await call.result, request.expectedEpoch);
          } finally {
            signal.removeEventListener('abort', cancel);
            call.cancel();
          }
        }),
      },
    }),
    {
      input: {request: input.request, observation: input.observation},
      clock: clockFrom(input.schedule),
    },
  );
}
