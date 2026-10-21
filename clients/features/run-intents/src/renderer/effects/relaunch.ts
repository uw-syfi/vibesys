import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge} from '@vibesys/ipc';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {
  HOST_CALL_METHOD,
  hostRelaunchRequest,
  hostRelaunchResponse,
  livenessRequest,
  livenessResponse,
  type RelaunchObservation,
  type RelaunchRequest,
  RUN_LIVENESS_METHOD,
  RunLiveness,
} from '@vibesys/runs/intents';
import {createActor, fromCallback, fromPromise} from 'xstate';
import {type RelaunchEvent, relaunchMachine} from '../../state/index.js';

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
        liveness: fromCallback<RelaunchEvent>(({sendBack}) => {
          const call = input.bridge.stream(
            input.host,
            RUN_LIVENESS_METHOD,
            livenessRequest(),
            bytes => {
              try {
                const value = livenessResponse(bytes, input.request.runId);
                if (value !== null) sendBack({type: 'liveness', value});
              } catch {
                sendBack({type: 'liveness', value: {epoch: 0n, value: RunLiveness.UNSPECIFIED}});
              }
            },
            () => sendBack({type: 'liveness', value: {epoch: 0n, value: RunLiveness.UNSPECIFIED}}),
          );
          return () => call.cancel();
        }),
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
