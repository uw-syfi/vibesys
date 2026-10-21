import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge, WelcomeBridge} from '@vibesys/ipc';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {TAIL_RECORDS_METHOD, tailRecordRows, tailRecordsRequest} from '@vibesys/run-state/records';
import {createActor, fromCallback, fromPromise} from 'xstate';
import {type GatewayRecordsEvent, gatewayRecordsMachine} from '../../state/index.js';

export function recordsActor(
  bridge: GatewayBridge,
  settings: WelcomeBridge,
  host: HostKey,
  runId: string,
  schedule: ScheduleTimeout,
) {
  const setting = async (enabled?: boolean): Promise<boolean> => {
    const reply = await settings.runTransportV2(host, enabled);
    if (!reply.ok) throw new Error(reply.error);
    return reply.value;
  };
  return createActor(
    gatewayRecordsMachine.provide({
      actors: {
        read: fromPromise(() => setting()),
        save: fromPromise(({input}) => setting(input.enabled)),
        tail: fromCallback<GatewayRecordsEvent>(({sendBack}) => {
          const call = bridge.stream(
            host,
            TAIL_RECORDS_METHOD,
            tailRecordsRequest(runId),
            bytes => {
              try {
                sendBack({type: 'records', records: tailRecordRows(bytes)});
              } catch (error) {
                sendBack({type: 'failed', error: String(error)});
              }
            },
            error => {
              if (error !== undefined) sendBack({type: 'failed', error});
            },
          );
          return () => call.cancel();
        }),
      },
    }),
    {clock: clockFrom(schedule)},
  );
}
