import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {gunzipSync} from 'node:zlib';
import {fromBinary} from '@bufbuild/protobuf';
import type {RunEvent} from '@vibesys/contracts';
import {RecordSchema} from '@vibesys/proto';
import {seekSkeleton} from '@vibesys/run-skeleton';
import {FakeSkeletonCache} from '@vibesys/run-skeleton/cache';
import {gatewayReader} from '@vibesys/run-skeleton/reader';
import {FakeRunTransport} from '@vibesys/run-skeleton/testing';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {readSkeleton} from '@vibesys/run-state/skeleton';
import {FakeClock} from '@vibesys/transport/testing';
import type {SnapshotFrom} from 'xstate';
import type {gatewayRunMachine} from '../../state/index.js';
import {startGatewayRun} from '../index.js';

const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'Local',
  unavailable: {},
};

function reached(
  actor: ReturnType<typeof startGatewayRun>,
  predicate: (snapshot: SnapshotFrom<typeof gatewayRunMachine>) => boolean,
): Promise<void> {
  if (predicate(actor.getSnapshot())) return Promise.resolve();
  return new Promise(resolve => {
    const subscription = actor.subscribe(snapshot => {
      if (predicate(snapshot)) {
        subscription.unsubscribe();
        resolve();
      }
    });
  });
}

for (const enabled of [false, true]) {
  test(`retained converted history follows the per-host switch (${enabled})`, async () => {
    const root = new URL('../../../../../../proto/testdata/conversion/', import.meta.url);
    const events: RunEvent[] = JSON.parse(
      gunzipSync(readFileSync(new URL('legacy.events.json.gz', root))).toString(),
    );
    const values: string[] = JSON.parse(
      gunzipSync(readFileSync(new URL('records.base64.json.gz', root))).toString(),
    );
    const records = values.map(value =>
      fromBinary(RecordSchema, Uint8Array.from(Buffer.from(value, 'base64'))),
    );
    const legacy = reduceEventBatch(initialCoreState(), events);
    const transport = new FakeRunTransport({records});
    const clock = new FakeClock();
    const actor = startGatewayRun(
      {
        reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
        watchSetting: () => () => {},
        setting: async value => value ?? enabled,
      },
      clock.schedule,
      {seed: SEED},
    );
    try {
      await reached(actor, snapshot =>
        enabled
          ? snapshot.context.load?.complete === true &&
            snapshot.context.ui.recordLog?.length === records.length
          : snapshot.matches('off'),
      );
      expect(actor.getSnapshot().context.enabled).toBe(enabled);
      if (!enabled) {
        expect(transport.calls).toHaveLength(0);
        expect(legacy.status).toBe('completed');
      } else {
        const load = actor.getSnapshot().context.load;
        if (!load) throw new Error('retained load required');
        const reading = readSkeleton(load.skeleton, seekSkeleton(load.skeleton, load.committedSeq));
        expect(String(reading.phase)).toBe(legacy.status);
        expect(
          actor.getSnapshot().context.ui.recordLog?.filter(row => row.body === 'notice'),
        ).toHaveLength(40);
      }
      actor.send({type: 'toggle', enabled: false});
      await reached(actor, snapshot => snapshot.matches('off'));
      expect(transport.activeStreams).toBe(0);
    } finally {
      actor.stop();
    }
  });
}
