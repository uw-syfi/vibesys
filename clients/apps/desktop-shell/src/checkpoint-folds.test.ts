import {expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';
import {create} from '@bufbuild/protobuf';
import {anyPack} from '@bufbuild/protobuf/wkt';
import {foldOutcomes} from '@vibesys/feature-run-intents/state';
import {startGatewayRun} from '@vibesys/feature-run-page/renderer';
import {emptySteerFold, foldSteer, steerDeliveries} from '@vibesys/feature-run-sessions/state';
import {
  AgentTurnAttrsSchema,
  Change,
  IntentAction,
  IntentStep,
  KeyframeSchema,
  RecordSchema,
  RuntimeType,
  Severity,
  SpanStatus,
  type Record as StoreRecord,
} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, RECORD_FACTS, recordsFanout, seedSkeleton} from '@vibesys/run-skeleton';
import {FakeSkeletonCache} from '@vibesys/run-skeleton/cache';
import {gatewayReader} from '@vibesys/run-skeleton/reader';
import {FakeRunTransport} from '@vibesys/run-skeleton/testing';
import {decodeRecordRows, recordRows} from '@vibesys/run-state/records';
import {intentOutcomes} from '@vibesys/runs/intents';
import {FakeClock} from '@vibesys/transport/testing';
import fc from 'fast-check';

const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'project', task: null},
  hostLabel: 'Local',
  unavailable: {},
};

function generatedLog(length: number, seed: number): StoreRecord[] {
  const records = [...storeRunFixture('live', 1, seed).records];
  let seq = records.at(-1)?.seq ?? 0n;
  const append = (body: StoreRecord['body']) =>
    records.push(
      create(RecordSchema, {
        seq: ++seq,
        epoch: 1,
        at: {seconds: seq},
        body,
      }),
    );
  append(
    create(RecordSchema, {
      body: {
        case: 'intent',
        value: {
          controlId: 'early-steer',
          action: IntentAction.STEER,
          step: IntentStep.APPLIED,
        },
      },
    }).body,
  );
  append(
    create(RecordSchema, {
      body: {
        case: 'span',
        value: {
          change: Change.STARTED,
          span: {
            id: 'delivery',
            name: 'delivery',
            runtimeType: RuntimeType.OPERATION,
            status: SpanStatus.RUNNING,
            label: {
              kind: 'agent_turn',
              links: [{rel: 'steer', to: 'early-steer'}],
              attrs: anyPack(
                AgentTurnAttrsSchema,
                create(AgentTurnAttrsSchema, {
                  agent: 'agent-a',
                  role: 'executor',
                  stage: 'execute',
                }),
              ),
            },
          },
        },
      },
    }).body,
  );
  append(
    create(RecordSchema, {
      body: {
        case: 'blob',
        value: {
          span: 'delivery',
          role: 'reply',
          blob: {sha256: 'a'.repeat(64), mediaType: 'text/plain'},
        },
      },
    }).body,
  );
  append(
    create(RecordSchema, {
      body: {case: 'notice', value: {severity: Severity.ERROR, code: 1, message: 'early failure'}},
    }).body,
  );
  while (records.length < length)
    append(
      create(RecordSchema, {
        body: {
          case: 'notice',
          value: {
            severity:
              seed % 2 === 0 && records.length % 17 === 0 ? Severity.ERROR : Severity.WARNING,
            code: 1,
            message: `work-${records.length}`,
          },
        },
      }).body,
    );
  return records;
}

async function folds(records: StoreRecord[], cut: number, previous = false) {
  const clock = new FakeClock();
  const prefix = foldSkeleton(seedSkeleton(), records.slice(0, cut));
  const keyframe =
    cut === 0
      ? undefined
      : create(KeyframeSchema, {...prefix, throughSeq: BigInt(cut), logOffset: 8n});
  if (keyframe && previous) {
    keyframe.recordsTracked = false;
    keyframe.failuresTracked = false;
    keyframe.intents = [];
    keyframe.recordFacts = [];
    keyframe.failures = [];
    for (const version of keyframe.epochs) {
      version.startsEpoch = false;
      version.startedAt = undefined;
    }
  }
  const peer = new FakeRunTransport({records, ...(keyframe ? {keyframe} : {})});
  const reader = gatewayReader(peer, new FakeSkeletonCache(), clock.schedule);
  const fanout = recordsFanout();
  let raw: ReturnType<typeof recordRows> = [];
  let intents: ReturnType<typeof foldOutcomes> = [];
  let steer = emptySteerFold;
  fanout.subscribe(batch => {
    raw = recordRows(raw, decodeRecordRows(batch));
    intents = foldOutcomes(intents, intentOutcomes(batch));
    steer = foldSteer(
      steer,
      batch.map(bytes => fromRecord(bytes)),
    );
  });
  const page = startGatewayRun(
    {
      reader: {
        ...reader,
        watch: (runId, receive, receiveRecords) =>
          reader.watch(runId, receive, batch => {
            receiveRecords(batch);
            fanout.publish(batch);
          }),
      },
    },
    clock.schedule,
    {seed: SEED},
  );
  const loaded = Promise.withResolvers<void>();
  const inspect = () => {
    const load = page.getSnapshot().context.load;
    if (
      load?.skeleton.throughSeq === BigInt(records.length) &&
      load.windowFrom === undefined &&
      raw.at(-1)?.seq === String(records.length)
    )
      loaded.resolve();
  };
  const subscription = page.subscribe(inspect);
  inspect();
  try {
    // The Bun test bound only guards a hung reader; publication, never elapsed time, settles this.
    await loaded.promise;
    return {
      skeleton: page.getSnapshot().context.load?.skeleton,
      raw,
      intents,
      deliveries: steerDeliveries(steer),
      replies: page.getSnapshot().context.replies,
      ui: page.getSnapshot().context.ui,
    };
  } finally {
    subscription.unsubscribe();
    page.stop();
    await peer.whenIdle();
  }
}

import {fromBinary} from '@bufbuild/protobuf';

function fromRecord(bytes: Uint8Array) {
  return fromBinary(RecordSchema, bytes);
}

test('class property: every shared-reader fold agrees at generated checkpoint cuts', async () => {
  await fc.assert(
    fc.asyncProperty(
      fc.integer({min: 20, max: 1600}),
      fc.nat(1600),
      fc.nat(20),
      async (length, position, seed) => {
        const records = generatedLog(length, seed);
        const cut = Math.min(position, records.length);
        const full = await folds(records, 0);
        const checkpoint = await folds(records, cut);
        expect(checkpoint).toEqual(full);
        expect(await folds(records, cut, true)).toEqual(full);
      },
    ),
    {
      numRuns: 12,
      seed: 341811,
      examples: [
        [1400, 1400, 0],
        [1400, 700, 1],
        [600, 600, 0],
      ],
    },
  );
});

/** New listeners must join this property, rather than silently depending on the raw replay window. */
test('shared-reader fold inventory is exhaustive', () => {
  const root = new URL('../../../features/', import.meta.url);
  function consumers(directory: URL): string[] {
    return readdirSync(directory, {withFileTypes: true}).flatMap(entry => {
      const path = new URL(`${entry.name}${entry.isDirectory() ? '/' : ''}`, directory);
      if (entry.isDirectory()) return consumers(path);
      if (!/\.[cm]?tsx?$/.test(entry.name) || entry.name.includes('.test.')) return [];
      const source = readFileSync(path, 'utf8');
      return /input\.records\(|return records\(batch|rows: decodeRecordRows\(records\)/.test(source)
        ? [path.pathname.slice(root.pathname.length)]
        : [];
    });
  }
  expect(consumers(root).sort()).toEqual([
    'run-intents/src/renderer/effects/controls.ts',
    'run-page/src/renderer/effects/gateway-run.ts',
    'run-sessions/src/renderer/effects/use-steer-deliveries.ts',
  ]);
  expect(Object.keys(RECORD_FACTS).sort()).toEqual([
    'blob',
    'chat',
    'declarationAmended',
    'epochStarted',
    'intent',
    'legacyEvent',
    'lifecycle',
    'metric',
    'notice',
    'span',
    'stream',
  ]);
});
