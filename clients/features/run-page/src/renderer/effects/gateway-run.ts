/** The feature supplies session operations to its machine; the session owns wire I/O. */
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import type {GatewayReader} from '@vibesys/run-skeleton/reader';
import {decodeRecordRows} from '@vibesys/run-state/records';
import {appendNativeTranscript, emptyNativeTranscript} from '@vibesys/run-state/transcript';
import {createActor, fromCallback, fromPromise} from 'xstate';
import {
  type GatewayEvent,
  type GatewayInput,
  type GatewayLogInput,
  type GatewayTranscriptInput,
  gatewayRunMachine,
} from '../../state/index.js';

export interface GatewayRunPorts {
  readonly reader: GatewayReader;
}
export function startGatewayRun(
  ports: GatewayRunPorts,
  schedule: ScheduleTimeout,
  input: GatewayInput,
) {
  return createActor(
    gatewayRunMachine.provide({
      actors: {
        loader: fromCallback<GatewayEvent, {runId: string}>(({input, sendBack}) =>
          ports.reader.watch(
            input.runId,
            ({load, progress, failure, terminal, hint, fresh}) =>
              sendBack({
                type: 'loaded',
                load,
                progress,
                failure,
                terminal,
                fresh,
                ...(hint ? {hint} : {}),
              }),
            records => sendBack({type: 'records', rows: decodeRecordRows(records)}),
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
        transcript: fromPromise<void, GatewayTranscriptInput>(async ({input, signal}) => {
          let transcript = emptyNativeTranscript();
          await ports.reader.binary(input.runId, input.streamId, signal, bytes => {
            if (signal.aborted) return;
            transcript = appendNativeTranscript(transcript, bytes);
            input.receive({type: 'transcript', spanId: input.spanId, transcript});
          });
          if (!signal.aborted && transcript.pending.length > 0)
            throw new Error('Truncated transcript');
        }),
      },
    }),
    {input, clock: clockFrom(schedule)},
  ).start();
}
