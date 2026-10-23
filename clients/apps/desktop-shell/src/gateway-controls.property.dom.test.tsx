import {afterAll, beforeAll, expect, test} from 'bun:test';
import {clone, create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {type HostKey, recordedProjectRoot} from '@vibesys/hosts';
import {FakeGatewayBridge} from '@vibesys/ipc/testing';
import {
  DrainGoal,
  HostCallResponseSchema,
  IntentAction,
  IntentStep,
  LifecycleSchema,
  Outcome,
  RecordSchema,
  RunLiveness,
  StageState,
  StartupStage,
  type Record as StoreRecord,
  TailRequestSchema,
  TailResponseSchema,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {FakeClock} from '@vibesys/transport/testing';
import {IDBFactory} from 'fake-indexeddb';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act, Profiler} from 'react';
import {createRoot} from 'react-dom/client';
import {GatewayRun} from './gateway-run.js';
import {type WireFixture, wire} from './testing/gateway-wire.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed)
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  Object.assign(globalThis, {window, indexedDB: new IDBFactory(), IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'indexedDB', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

/** The same protobuf peer as the app journeys, through the Electron byte bridge role. */
class RunPeer extends FakeGatewayBridge {
  silent = false;
  constructor(
    readonly input: WireFixture,
    readonly records: StoreRecord[],
    readonly loading: boolean,
  ) {
    super();
  }
  override unary(host: HostKey, method: string, request: Uint8Array) {
    const call = super.unary(host, method, request);
    const reply =
      method === this.input.methods.skeleton
        ? this.input.skeleton
        : method === this.input.methods.blobs
          ? this.input.blobs[[...request].join(',')]
          : undefined;
    if (method === this.input.methods.hostCall) return call;
    if (!reply) throw new Error(`Unexpected unary ${method}`);
    this.calls.at(-1)?.resolve(new Uint8Array(reply));
    return call;
  }
  override stream(
    host: HostKey,
    method: string,
    request: Uint8Array,
    receive: (bytes: Uint8Array) => void,
    ended: (error?: string) => void,
  ) {
    const owned = super.stream(host, method, request, receive, ended);
    if (this.silent && method === this.input.methods.watchRuns) return owned;
    if (method === this.input.methods.tail) {
      const tail = fromBinary(TailRequestSchema, request);
      const records = this.records.filter(record => record.seq >= tail.fromSeq);
      if (!tail.sessionId && records.length > 0 && !(this.loading && tail.fromSeq > 0n))
        receive(
          toBinary(
            TailResponseSchema,
            create(TailResponseSchema, {
              records: records.map(record => toBinary(RecordSchema, record)),
              throughSeq: records.at(-1)?.seq ?? 0n,
            }),
          ),
        );
    } else if (method === this.input.methods.watchRuns)
      receive(new Uint8Array(this.input.watchRuns));
    else if (method === this.input.methods.log) receive(new Uint8Array(this.input.log));
    else throw new Error(`Unexpected stream ${method}`);
    return owned;
  }
}

const LIVE = fc.constantFrom(
  create(LifecycleSchema, {
    state: {
      case: 'starting',
      value: {stage: StartupStage.PROBING_WORKSPACE, state: StageState.RUNNING},
    },
  }),
  create(LifecycleSchema, {state: {case: 'running', value: {}}}),
  create(LifecycleSchema, {
    state: {case: 'running', value: {hold: {reason: {case: 'operator', value: {}}}}},
  }),
  create(LifecycleSchema, {state: {case: 'draining', value: {goal: DrainGoal.PARK}}}),
);
const STATE = fc.oneof(
  LIVE,
  fc.constant(create(LifecycleSchema, {state: {case: 'parked', value: {}}})),
  fc
    .record({
      outcome: fc.constantFrom(
        Outcome.FINISHED,
        Outcome.FAILED,
        Outcome.STOPPED,
        Outcome.INTERRUPTED,
      ),
      resumable: fc.boolean(),
    })
    .map(value => create(LifecycleSchema, {state: {case: 'ended', value}})),
);

const BACKGROUND = fc.record({
  epoch: fc.integer({min: 1, max: 8}),
  // The gateway states a head's epoch from the committed log, so no reader sees one without it.
  modernHead: fc.constant(true),
  history: fc.array(
    fc.record({
      action: fc.constantFrom(
        IntentAction.PAUSE,
        IntentAction.PAUSE_NOW,
        IntentAction.RESUME,
        IntentAction.STEER,
      ),
      applied: fc.boolean(),
    }),
    {maxLength: 6},
  ),
});
type Generated<A> = A extends fc.Arbitrary<infer Value> ? Value : never;
type Scenario = Generated<typeof BACKGROUND> & {
  lifecycle: Generated<typeof STATE>;
  liveness: RunLiveness;
  loading: boolean;
};
const LIVENESS = fc.constantFrom(
  RunLiveness.ALIVE,
  RunLiveness.INTERRUPTED,
  RunLiveness.ENDED,
  RunLiveness.UNSPECIFIED,
);

function peerFor({lifecycle, liveness, epoch, loading, history, modernHead}: Scenario): RunPeer {
  const source = storeRunFixture('live');
  const records = source.records.map(record =>
    create(RecordSchema, {
      ...record,
      epoch,
      body:
        record.body.case === 'epochStarted'
          ? {case: 'epochStarted', value: {...record.body.value, epoch}}
          : record.body.case === 'lifecycle'
            ? {case: 'lifecycle', value: lifecycle}
            : record.body,
    }),
  );
  const last = records.at(-1)?.seq ?? 0n;
  records.push(
    ...history.map((item, index) =>
      create(RecordSchema, {
        seq: last + BigInt(index + 1),
        epoch,
        body: {
          case: 'intent',
          value: {
            controlId: `history-${index}`,
            action: item.action,
            step: item.applied ? IntentStep.APPLIED : IntentStep.REFUSED,
            ...(!item.applied ? {reason: 'Unsupported by an older host'} : {}),
          },
        },
      }),
    ),
  );
  const fixture = {
    ...source,
    records,
    initialCount: loading ? source.initialCount : records.length,
  };
  const input = wire(fixture, liveness);
  if (modernHead) {
    const observed = fromBinary(WatchRunsResponseSchema, new Uint8Array(input.watchRuns));
    if (!observed.head) throw new Error('head required');
    observed.head.epoch = epoch;
    input.watchRuns = [...toBinary(WatchRunsResponseSchema, observed)];
  }
  return new RunPeer(input, records, loading);
}

const NAVIGATION = {runAgain: () => undefined, openWindow: () => undefined};
const ADDRESS = {
  fragment: () => '',
  push: () => {},
  onRestore: () => () => {},
  onEscape: () => () => {},
};
async function verifyOffers(input: Scenario) {
  const peer = peerFor(input);
  const clock = new FakeClock();
  const container = document.createElement('div');
  const root = createRoot(container);
  let renders = 0;
  try {
    await act(async () =>
      root.render(
        <Profiler
          id="run"
          onRender={() => {
            renders += 1;
          }}
        >
          <GatewayRun
            projectRoot={recordedProjectRoot('/project')}
            seed={{
              identity: {host: 'local' as HostKey, runId: 'run-a'},
              title: {project: '/project', task: 'mpmc'},
              hostLabel: 'host-a',
              unavailable: {},
            }}
            bridge={peer}
            schedule={clock.schedule}
            address={ADDRESS}
            navigation={NAVIGATION}
          />
        </Profiler>,
      ),
    );
    const buttons = [...container.querySelectorAll('button')].map(button => button.textContent);
    expect(buttons).not.toContain('Stop');
    const state = input.lifecycle.state;
    const live = state.case === 'starting' || state.case === 'running' || state.case === 'draining';
    // What the host would admit: live actions need a writer that is not dead, and Resume needs
    // one that is not alive; every ending is admitted, whatever flag the ended record keeps.
    const dead = input.liveness === RunLiveness.INTERRUPTED;
    const alive = input.liveness === RunLiveness.ALIVE;
    expect(buttons.includes('Pause now')).toBe(live && alive);
    expect(buttons.includes('Pause')).toBe(
      live &&
        alive &&
        (state.case === 'starting' ||
          (state.case === 'running' && state.value.hold?.reason.case !== 'operator')),
    );
    expect(buttons.includes('Guidance')).toBe(state.case === 'running' && alive);
    expect(buttons.includes('Resume')).toBe(
      input.liveness !== RunLiveness.ALIVE && (!live || dead),
    );
    // Chat steers are the Guidance control's twin: one permission, whichever surface offers it.
    // Ordinary questions stay available whatever the writer is.
    const chat = container.querySelector('button[aria-label="Chat"]');
    if (chat) {
      await act(async () => (chat as HTMLElement).click());
      const chatButtons = [...container.querySelectorAll('button')].map(
        button => button.textContent,
      );
      expect(chatButtons.includes('New steer')).toBe(buttons.includes('Guidance'));
      expect(chatButtons).toContain('New chat');
    }
    if (!input.loading) {
      expect(container.querySelector('[inert] button') === null).toBe(true);
    }
    expect(renders).toBeLessThan(40);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
  expect(peer.streams.every(stream => stream.cancelled)).toBe(true);
}

test('property: every loading live run exposes the controls its writer admits through the real app bridge', async () => {
  await fc.assert(
    fc.asyncProperty(LIVE, LIVENESS, BACKGROUND, async (lifecycle, liveness, background) => {
      await verifyOffers({...background, lifecycle, liveness, loading: true});
    }),
    {
      numRuns: 24,
      seed: 226,
      examples: [
        [
          create(LifecycleSchema, {
            state: {
              case: 'starting',
              value: {
                stage: StartupStage.PROBING_WORKSPACE,
                state: StageState.RUNNING,
              },
            },
          }),
          RunLiveness.ALIVE,
          {epoch: 1, modernHead: true, history: []},
        ],
      ],
    },
  );
});

test('property: ready run controls follow the writer lifecycle and liveness, including interrupted writers and every ended run', async () => {
  await fc.assert(
    fc.asyncProperty(STATE, LIVENESS, BACKGROUND, async (lifecycle, liveness, background) => {
      await verifyOffers({...background, lifecycle, liveness, loading: false});
    }),
    {numRuns: 32, seed: 229},
  );
});

async function withPeer(
  peer: RunPeer,
  work: (container: HTMLElement, clock: FakeClock) => Promise<void>,
  location = {projectRoot: recordedProjectRoot('/project')},
) {
  const clock = new FakeClock();
  const container = document.createElement('div');
  const root = createRoot(container);
  try {
    await act(async () =>
      root.render(
        <GatewayRun
          projectRoot={location.projectRoot}
          seed={{
            identity: {host: 'local' as HostKey, runId: 'run-a'},
            title: {project: '/project', task: 'mpmc'},
            hostLabel: 'host-a',
            unavailable: {},
          }}
          bridge={peer}
          schedule={clock.schedule}
          address={ADDRESS}
          navigation={NAVIGATION}
        />,
      ),
    );
    await work(container, clock);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
  expect(peer.streams.every(stream => stream.cancelled)).toBe(true);
  expect(clock.pending).toBe(0);
}

const controlButtons = (container: HTMLElement) =>
  [...container.querySelectorAll('button')]
    .map(button => button.textContent ?? '')
    .filter(text => ['Pause', 'Pause now', 'Guidance', 'Resume'].includes(text));

const FAULT = fc.record({
  // The intent history shares the lifecycle's reader: one records stream, not two.
  source: fc.constantFrom('liveness', 'lifecycle'),
  fault: fc.constantFrom('end', 'stall'),
  elapsed: fc.integer({min: 0, max: 31_000}),
});
type Fault = Generated<typeof FAULT>;

function followers(peer: RunPeer, source: Fault['source']) {
  return peer.streams.filter(stream => {
    if (stream.cancelled) return false;
    if (source === 'liveness') return stream.method === peer.input.methods.watchRuns;
    if (stream.method !== peer.input.methods.tail) return false;
    const request = fromBinary(TailRequestSchema, stream.bytes);
    return !request.sessionId && request.fromSeq > 0n;
  });
}

function assertVisible(container: HTMLElement) {
  if (controlButtons(container).length === 0)
    expect(container.textContent).toMatch(
      /catching up|unknown|waiting for the host|not confirmed/i,
    );
}

test('property: independently ending or stalling streams cannot revive a dead writer and recover within one virtual minute', async () => {
  await fc.assert(
    fc.asyncProperty(
      LIVE,
      fc.array(FAULT, {minLength: 1, maxLength: 5}),
      async (lifecycle, faults) => {
        const peer = peerFor({
          lifecycle,
          liveness: RunLiveness.INTERRUPTED,
          epoch: 1,
          loading: false,
          history: [],
          modernHead: true,
        });
        await withPeer(peer, async (container, clock) => {
          for (const fault of faults) {
            const before = peer.streams.length;
            const old = followers(peer, fault.source);
            await act(async () => {
              if (fault.fault === 'end') for (const stream of old) stream.ended('disconnected');
            });
            expect(
              controlButtons(container).some(text =>
                ['Pause', 'Pause now', 'Guidance'].includes(text),
              ),
            ).toBe(false);
            assertVisible(container);
            await act(async () => clock.advanceBy(fault.elapsed));
            expect(
              controlButtons(container).some(text =>
                ['Pause', 'Pause now', 'Guidance'].includes(text),
              ),
            ).toBe(false);
            assertVisible(container);
            await act(async () => clock.advanceBy(31_000));
            expect(peer.streams.length).toBeGreaterThan(before);
            assertVisible(container);
            const recovered = controlButtons(container);
            // Late bytes from retired subscriptions are fenced by their owning actor.
            await act(async () => {
              for (const stream of old.filter(stream => stream.cancelled)) {
                if (stream.method === peer.input.methods.watchRuns)
                  stream.receive(
                    toBinary(
                      WatchRunsResponseSchema,
                      create(WatchRunsResponseSchema, {
                        runId: 'run-a',
                        head: {epoch: 1},
                        liveness: RunLiveness.ALIVE,
                      }),
                    ),
                  );
              }
            });
            expect(controlButtons(container)).toEqual(recovered);
          }
        });
      },
    ),
    {
      numRuns: 16,
      seed: 34361,
      examples: [
        [
          create(LifecycleSchema, {state: {case: 'running', value: {}}}),
          [{source: 'liveness', fault: 'end', elapsed: 0}],
        ],
      ],
    },
  );
});

const GAP = fc.record({
  epoch: fc.integer({min: 2, max: 8}),
  lifecycle: LIVE,
  faults: fc.array(FAULT, {maxLength: 3}),
  lead: fc.constantFrom('history', 'liveness', 'ack'),
});

test('property: a successor on any independent stream exposes a stalled lifecycle and re-reads it within one virtual minute', async () => {
  await fc.assert(
    fc.asyncProperty(GAP, async ({epoch, lifecycle, faults, lead}) => {
      const peer = peerFor({
        lifecycle: create(LifecycleSchema, {state: {case: 'parked', value: {}}}),
        liveness: RunLiveness.ENDED,
        epoch: 1,
        loading: false,
        history: [],
        modernHead: true,
      });
      await withPeer(peer, async (container, clock) => {
        const sequence = peer.records.at(-1)?.seq ?? 0n;
        const header = peer.records.find(record => record.body.case === 'epochStarted');
        if (header?.body.case !== 'epochStarted') throw new Error('epoch header required');
        const successor = create(RecordSchema, {
          seq: sequence + 1n,
          epoch,
          body: {case: 'epochStarted', value: {...header.body.value, epoch}},
        });
        const next = create(RecordSchema, {
          seq: sequence + 2n,
          epoch,
          body: {case: 'lifecycle', value: lifecycle},
        });
        const snapshot = wire(
          {
            ...storeRunFixture('live'),
            records: [...peer.records, successor, next],
            initialCount: peer.records.length + 2,
          },
          RunLiveness.ALIVE,
        );
        // The gateway states the epoch of every head it sends.
        const stated = fromBinary(WatchRunsResponseSchema, new Uint8Array(snapshot.watchRuns));
        if (!stated.head) throw new Error('head required');
        stated.head.epoch = epoch;
        snapshot.watchRuns = [...toBinary(WatchRunsResponseSchema, stated)];
        // The host has the successor, while the already-open lifecycle follower stalls.
        peer.input.skeleton = snapshot.skeleton;
        peer.input.watchRuns = snapshot.watchRuns;
        const before = peer.calls.filter(
          call => call.method === peer.input.methods.skeleton,
        ).length;
        await act(async () => deliverLead(peer, container, lead, successor, snapshot));
        if (lead !== 'liveness') {
          expect(controlButtons(container)).toEqual([]);
          expect(container.textContent).toMatch(/catching up/i);
        }
        assertVisible(container);
        for (const fault of faults) {
          await act(async () => {
            if (fault.fault === 'end')
              for (const stream of followers(peer, fault.source)) stream.ended('disconnected');
          });
          assertVisible(container);
          await act(async () => clock.advanceBy(fault.elapsed));
          assertVisible(container);
        }
        await act(async () => clock.advanceBy(31_000));
        expect(
          peer.calls.filter(call => call.method === peer.input.methods.skeleton).length,
        ).toBeGreaterThan(before);
        await act(async () => {
          clock.advanceBy(1000);
          for (const stream of followers(peer, 'liveness'))
            stream.receive(new Uint8Array(snapshot.watchRuns));
        });
        const buttons = controlButtons(container);
        expect(buttons).toContain('Pause now');
        expect(buttons).not.toContain('Resume');
        assertVisible(container);
      });
    }),
    {
      numRuns: 16,
      seed: 34362,
      examples: [
        [
          {
            epoch: 2,
            lifecycle: create(LifecycleSchema, {state: {case: 'running', value: {}}}),
            faults: [],
            lead: 'history',
          },
        ],
      ],
    },
  );
});

function deliverLead(
  peer: RunPeer,
  container: HTMLElement,
  lead: 'history' | 'liveness' | 'ack',
  successor: StoreRecord,
  snapshot: WireFixture,
) {
  if (lead === 'history')
    for (const stream of followers(peer, 'lifecycle'))
      stream.receive(
        toBinary(
          TailResponseSchema,
          create(TailResponseSchema, {records: [toBinary(RecordSchema, successor)]}),
        ),
      );
  else if (lead === 'liveness')
    for (const stream of followers(peer, 'liveness'))
      stream.receive(new Uint8Array(snapshot.watchRuns));
  else {
    const resume = [...container.querySelectorAll('button')].find(
      button => button.textContent === 'Resume',
    );
    if (!resume) throw new Error('Resume required');
    resume.click();
    peer.calls.at(-1)?.resolve(
      toBinary(
        HostCallResponseSchema,
        create(HostCallResponseSchema, {
          result: {
            case: 'relaunch',
            value: {outcome: {case: 'epoch', value: BigInt(successor.epoch)}},
          },
        }),
      ),
    );
  }
}

test('property: every offered writer control changes visible content across lifecycle, liveness and history states', async () => {
  const exercised = new Set<string>();
  await fc.assert(
    fc.asyncProperty(STATE, LIVENESS, BACKGROUND, async (lifecycle, liveness, background) => {
      const scenario = {...background, lifecycle, liveness, loading: false};
      const labels: string[] = [];
      await withPeer(peerFor(scenario), async container => {
        labels.push(...controlButtons(container));
      });
      for (const label of labels) {
        exercised.add(label);
        await withPeer(peerFor(scenario), async container => {
          const button = [...container.querySelectorAll('button')].find(
            each => each.textContent === label,
          );
          if (!button) throw new Error(`Missing ${label}`);
          const before = container.textContent;
          await act(async () => button.click());
          expect(container.textContent, label).not.toBe(before);
        });
      }
    }),
    {numRuns: 32, seed: 34364},
  );
  expect([...exercised].sort()).toEqual(['Guidance', 'Pause', 'Pause now', 'Resume']);
});

test('property: optional recorded facts never erase the catalog-addressed controls or the missing-location reason', async () => {
  await fc.assert(
    fc.asyncProperty(
      STATE,
      BACKGROUND,
      fc.boolean(),
      fc.boolean(),
      async (lifecycle, background, recorded, addressed) => {
        const peer = peerFor({
          ...background,
          lifecycle,
          liveness: RunLiveness.ENDED,
          loading: false,
        });
        if (!recorded) {
          const source = storeRunFixture('live');
          const fixture = {
            ...source,
            initialCount: peer.records.length,
            records: peer.records.map(record => {
              if (record.body.case !== 'epochStarted') return record;
              // Only the optional recorded location goes; the declaration stays a valid one.
              const copy = clone(RecordSchema, record);
              if (copy.body.case === 'epochStarted') copy.body.value.projectRoot = undefined;
              return copy;
            }),
          };
          const replacement = wire(fixture, RunLiveness.ENDED);
          peer.input.skeleton = replacement.skeleton;
          peer.records.splice(0, peer.records.length, ...fixture.records);
        }
        await withPeer(
          peer,
          async container => {
            const buttons = controlButtons(container);
            if (!addressed) {
              expect(buttons).toEqual([]);
              expect(container.textContent).toContain('Waiting for the host to locate this run');
            } else if (lifecycle.state.case === 'ended' || lifecycle.state.case === 'parked') {
              expect(buttons).toEqual(['Resume']);
              const before = container.textContent;
              await act(async () =>
                [...container.querySelectorAll('button')]
                  .find(button => button.textContent === 'Resume')
                  ?.click(),
              );
              expect(container.textContent).not.toBe(before);
            } else assertVisible(container);
          },
          {projectRoot: addressed ? recordedProjectRoot('/catalog/project') : undefined},
        );
      },
    ),
    {
      numRuns: 16,
      seed: 34367,
      examples: [
        [
          create(LifecycleSchema, {state: {case: 'ended', value: {outcome: Outcome.FINISHED}}}),
          {epoch: 1, modernHead: true, history: []},
          false,
          true,
        ],
        [
          create(LifecycleSchema, {state: {case: 'parked', value: {}}}),
          {epoch: 1, modernHead: true, history: []},
          true,
          false,
        ],
      ],
    },
  );
});

const RECEIPT = fc.record({
  kind: fc.constantFrom('frame', 'freeze', 'close', 'resume'),
  elapsed: fc.integer({min: 0, max: 35000}),
});

async function receiveHostStep(
  peer: RunPeer,
  step: Generated<typeof RECEIPT>,
  state: {frozen: boolean; lastFrame: number},
  now: number,
) {
  if (step.kind === 'freeze') state.frozen = true;
  if (step.kind === 'resume') state.frozen = false;
  const streams = followers(peer, 'liveness');
  if (step.kind === 'close') {
    state.lastFrame = -Infinity;
    await act(async () => {
      for (const stream of streams) stream.ended('closed');
    });
  } else if (
    ((step.kind === 'frame' && !state.frozen) || step.kind === 'resume') &&
    streams.length
  ) {
    state.lastFrame = now;
    await act(async () => {
      for (const stream of streams) stream.receive(new Uint8Array(peer.input.watchRuns));
    });
  }
}

test('property: host byte receipts alone renew the page controls lease across freeze, close and resume', async () => {
  await fc.assert(
    fc.asyncProperty(fc.array(RECEIPT, {minLength: 1, maxLength: 4}), async steps => {
      const peer = peerFor({
        epoch: 1,
        lifecycle: create(LifecycleSchema, {
          state: {case: 'running', value: {}},
        }),
        liveness: RunLiveness.ALIVE,
        loading: false,
        history: [],
        modernHead: true,
      });
      await withPeer(peer, async (container, clock) => {
        peer.silent = true;
        const state = {frozen: false, lastFrame: clock.now()};
        const check = () => {
          const live = controlButtons(container).includes('Pause');
          expect(live).toBe(clock.now() - state.lastFrame < 30000);
          assertVisible(container);
        };
        for (const step of steps) {
          await receiveHostStep(peer, step, state, clock.now());
          check();
          for (let elapsed = 0; elapsed < step.elapsed; elapsed += 1000) {
            await act(async () => clock.advanceBy(Math.min(1000, step.elapsed - elapsed)));
            check();
          }
        }
      });
    }),
    {
      numRuns: 12,
      seed: 341809,
      examples: [
        [
          [
            {kind: 'freeze', elapsed: 35000},
            {kind: 'resume', elapsed: 0},
          ],
        ],
      ],
    },
  );
});

function statusWord(container: HTMLElement): string | undefined {
  return [...container.querySelectorAll('span')]
    .map(element => element.textContent ?? '')
    .find(text => ['Starting', 'Running', 'Paused', 'Stopped', 'Failed'].includes(text));
}

test('property: relaunch headers and controls share the committed epoch even when history and older heads arrive late', async () => {
  await fc.assert(
    fc.asyncProperty(
      fc.integer({min: 1, max: 8}),
      fc.integer({min: 1, max: 4}),
      async (epoch, count) => {
        const peer = peerFor({
          epoch,
          lifecycle: create(LifecycleSchema, {
            state: {case: 'ended', value: {outcome: Outcome.STOPPED}},
          }),
          liveness: RunLiveness.ENDED,
          loading: false,
          history: [],
          modernHead: true,
        });
        await withPeer(peer, async (container, clock) => {
          peer.silent = true;
          let seq = peer.records.at(-1)?.seq ?? 0n;
          for (let index = 1; index <= count; index += 1) {
            const nextEpoch = epoch + index;
            const packet = (running: boolean) =>
              toBinary(
                WatchRunsResponseSchema,
                create(WatchRunsResponseSchema, {
                  runId: 'run-a',
                  liveness: RunLiveness.ALIVE,
                  head: {
                    epoch: nextEpoch,
                    committedSeq: seq + (running ? 2n : 1n),
                    lifecycle: {
                      state: running ? {case: 'running', value: {}} : {case: 'starting', value: {}},
                    },
                  },
                }),
              );
            const deliver = async (bytes: Uint8Array) =>
              act(async () => {
                for (const stream of followers(peer, 'liveness')) stream.receive(bytes);
              });
            await deliver(packet(false));
            expect(statusWord(container)).toBe('Starting');
            expect(controlButtons(container)).toEqual(['Pause', 'Pause now']);
            await deliver(packet(true));
            expect(statusWord(container)).toBe('Running');
            expect(controlButtons(container)).toEqual(['Pause', 'Pause now', 'Guidance']);
            await deliver(packet(false));
            expect(statusWord(container)).toBe('Running');
            await act(async () => clock.advanceBy(30000));
            expect(statusWord(container)).toBe('Running');
            expect(controlButtons(container)).toEqual([]);
            assertVisible(container);
            await act(async () => clock.advanceBy(1000));
            await deliver(packet(true));
            expect(statusWord(container)).toBe('Running');
            expect(controlButtons(container)).toEqual(['Pause', 'Pause now', 'Guidance']);
            seq += 2n;
          }
        });
      },
    ),
    {numRuns: 8, seed: 341810, examples: [[1, 1]]},
  );
});
