import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
import {clone, create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {TimestampSchema} from '@bufbuild/protobuf/wkt';
import {RunIntents} from '@vibesys/feature-run-intents/renderer';
import {RunScreen, useGatewayRun} from '@vibesys/feature-run-page/renderer';
import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge} from '@vibesys/ipc';
import {
  HostCallRequestSchema,
  HostCallResponseSchema,
  LifecycleSchema,
  Outcome,
  RecordSchema,
  RelaunchRefusal,
} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, type SkeletonLoad, seedSkeleton} from '@vibesys/run-skeleton';
import type {GatewayReader} from '@vibesys/run-skeleton/reader';
import type {RelaunchTarget} from '@vibesys/runs/intents';
import {FakeClock} from '@vibesys/transport/testing';
import {Text} from '@vibesys/ui';
import {createRoot} from 'react-dom/client';

const query = new URLSearchParams(window.location.search);
const observed = query.get('state') ?? 'interrupted';
if (!['parked', 'stopped-resumable', 'interrupted'].includes(observed))
  throw new Error('Invalid state');
const target: RelaunchTarget = {
  epoch: 1n,
  state: observed as RelaunchTarget['state'],
};
const clock = new FakeClock();
const fixture = storeRunFixture('short', 2, 3);
const records = fixture.records.map(record => clone(RecordSchema, record));
const last = records.at(-1);
if (last?.body.case !== 'lifecycle') throw new Error('Missing lifecycle');
last.body.value = create(LifecycleSchema, {
  state:
    observed === 'parked'
      ? {case: 'parked', value: {}}
      : {
          case: 'ended',
          value: {outcome: observed === 'interrupted' ? Outcome.INTERRUPTED : Outcome.STOPPED},
        },
});
let skeleton = foldSkeleton(seedSkeleton(), records);
const readers = new Set<(load: SkeletonLoad | undefined, failed: boolean) => void>();
const load = (): SkeletonLoad => ({
  skeleton,
  committedSeq: skeleton.throughSeq ?? 0n,
  nextSeq: (skeleton.throughSeq ?? 0n) + 1n,
  complete: true,
});
const reader: GatewayReader = {
  watch(_runId, receive) {
    readers.add(receive);
    receive(load(), false);
    return () => {
      readers.delete(receive);
    };
  },
  async content(_runId, hash) {
    return {hash, text: new TextDecoder().decode(fixture.blobs[hash])};
  },
  async log() {},
};
let reply: ((bytes: Uint8Array) => void) | null = null;
let cancelled = 0;
const bridge: GatewayBridge = {
  unary(_host, method, bytes) {
    if (!method.endsWith('/HostCall')) throw new Error('Invalid method');
    const request = fromBinary(HostCallRequestSchema, bytes);
    if (request.method.case !== 'relaunch' || request.method.value.expectedEpoch !== 1n)
      throw new Error('Invalid relaunch');
    let resolve: (bytes: Uint8Array) => void = () => {};
    const promise = new Promise<Uint8Array>(settle => {
      resolve = settle;
    });
    reply = resolve;
    return {
      result: promise,
      cancel: () => {
        cancelled += 1;
      },
    };
  },
  stream() {
    return {cancel() {}};
  },
};
function release(): void {
  const refusal = {
    locked: RelaunchRefusal.LOCKED,
    ended: RelaunchRefusal.ENDED,
    checkout: RelaunchRefusal.CHECKOUT_MISSING,
    epoch: RelaunchRefusal.EPOCH_CHANGED,
  }[query.get('result') ?? ''];
  if (refusal === undefined) {
    const first = records.find(record => record.body.case === 'epochStarted');
    if (!first) throw new Error('Missing epoch header');
    const epoch = clone(RecordSchema, first);
    epoch.seq = (skeleton.throughSeq ?? 0n) + 1n;
    epoch.epoch = 2;
    epoch.at = create(TimestampSchema, {seconds: (last?.at?.seconds ?? 0n) + 3600n});
    if (last?.clock !== undefined) epoch.clock = last.clock;
    if (epoch.body.case !== 'epochStarted') throw new Error('Invalid epoch');
    epoch.body.value.epoch = 2;
    epoch.body.value.treeHash = 'b'.repeat(40);
    skeleton = foldSkeleton(skeleton, [epoch]);
    for (const receive of readers) receive(load(), false);
  }
  reply?.(
    toBinary(
      HostCallResponseSchema,
      create(HostCallResponseSchema, {
        result: {
          case: 'relaunch',
          value: {
            outcome:
              refusal === undefined
                ? {case: 'epoch', value: 2n}
                : {case: 'refusal', value: refusal},
          },
        },
      }),
    ),
  );
}
Object.assign(window, {
  relaunchFixture: {
    release,
    timeout: () => clock.advanceBy(35_000),
    epochs: () => skeleton.epochs.length,
    cancelled: () => cancelled,
  },
});
const seed = {
  identity: {host: 'local' as HostKey, runId: 'run-a'},
  title: {project: 'queue-rs', task: 'mpmc'},
  hostLabel: 'Local',
  unavailable: {},
};
const ports = {reader, setting: async () => true};
const address = {
  fragment: () => '',
  push: () => {},
  onRestore: () => () => {},
  onEscape: () => () => {},
};
function Replay() {
  const page = useGatewayRun({seed, ports, schedule: clock.schedule, address});
  if (!page.state || !page.model || !page.whole)
    return <Text>{page.projectionError ?? JSON.stringify(page.snapshot.value)}</Text>;
  return (
    <RunScreen
      state={page.state}
      model={page.model}
      whole={page.whole}
      core={page.state.run.core}
      route={page.route}
      zoom={page.zoom}
      dispatch={page.dispatch}
      supplemental={
        <RunIntents
          bridge={bridge}
          host={seed.identity.host}
          projectRoot="/work/queue-rs"
          runId="run-a"
          enabled
          schedule={clock.schedule}
          {...(query.has('before') ? {} : {relaunch: target})}
        />
      }
    />
  );
}
const root = document.getElementById('root');
if (!root) throw new Error('Missing root');
createRoot(root).render(<Replay />);
