/** The feature supplies session operations to its machine; the session owns wire I/O. */
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import type {GatewayReader} from '@vibesys/run-skeleton/reader';
import {createActor, fromCallback, fromPromise} from 'xstate';
import {
  type GatewayEvent,
  type GatewayInput,
  type GatewayLogInput,
  gatewayRunMachine,
} from '../../state/index.js';

export interface GatewayRunPorts {
  readonly reader: GatewayReader;
  setting(enabled: boolean | undefined): Promise<boolean>;
}
export function startGatewayRun(
  ports: GatewayRunPorts,
  schedule: ScheduleTimeout,
  input: GatewayInput,
) {
  return createActor(
    gatewayRunMachine.provide({
      actors: {
        setting: fromPromise(({input}) => ports.setting(input)),
        loader: fromCallback<GatewayEvent, {runId: string}>(({input, sendBack}) =>
          ports.reader.watch(input.runId, (load, failed, hint) =>
            sendBack({type: 'loaded', load, failed, ...(hint ? {hint} : {})}),
          ),
        ),
        content: fromPromise(({input, signal}) =>
          ports.reader.content(input.runId, input.hash, signal),
        ),
        log: fromPromise<void, GatewayLogInput>(async ({input, signal}) => {
          try {
            await ports.reader.log(input.runId, input.streamId, signal, chunk =>
              input.receive({type: 'log', streamId: input.streamId, ...chunk}),
            );
          } catch {
            if (!signal.aborted) input.receive({type: 'log-failed'});
          }
        }),
      },
    }),
    {input, clock: clockFrom(schedule)},
  ).start();
}
