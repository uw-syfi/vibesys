import {afterAll, beforeAll, expect, test} from 'bun:test';
import {clone, create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {type HostKey, recordedProjectRoot} from '@vibesys/hosts';
import {FakeGatewayBridge} from '@vibesys/ipc/testing';
import {createProblemBoard} from '@vibesys/problems';
import {
  DrainGoal,
  HostCallResponseSchema,
  IntentAction,
  IntentStep,
  LifecycleSchema,
  Outcome,
  RecordSchema,
  type RunEntry,
  RunEntrySchema,
  RunLiveness,
  StageState,
  StartupStage,
  type Record as StoreRecord,
  TailRequestSchema,
  TailResponseSchema,
  TopicEntrySchema,
} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {FakeSubscriptionPort, type FakePortCall} from '@vibesys/subscription/testing';
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

const RUN_ID = 'run-a';

/**
 * Main's side of the window's subscription to the host's runs: it says what the host says of the
 * run to each observation, as main relays a subscription that is delivering, and says it has
 * stopped delivering when asked. The window holds no lease and no timer of its own for this.
 */
class LivenessHost {
  readonly port = new FakeSubscriptionPort();
  #delivering = true;
  #since = 0;

  constructor(public run: RunEntry) {
    const observe = this.port.observe;
    this.port.observe = (host, request, key, receive, ended) => {
      const handle = observe(host, request, key, receive, ended);
      // Main answers after the window has the handle, as it does over a port.
      queueMicrotask(() => {
        const call = this.port.calls.find(each => each.open && each.request === request);
        if (call !== undefined && this.#delivering) this.#say(call);
      });
      return handle;
    };
  }

  /** The observations of the run the window holds now. */
  get followers(): FakePortCall[] {
    return this.port.calls.filter(call => call.open);
  }

  #say(call: FakePortCall): void {
    this.#since += 1;
    call.send({kind: 'begin'});
    call.send({
      kind: 'entry',
      entry: create(TopicEntrySchema, {key: RUN_ID, value: {case: 'runs', value: this.run}}),
    });
    call.send({kind: 'synced'});
    call.send({kind: 'control', control: {kind: 'live', since: this.#since}});
  }

  /** The host says `run` of the run, on every observation: a stream that delivers. */
  publish(run: RunEntry = this.run): void {
    this.run = run;
    this.#delivering = true;
    for (const call of this.followers) this.#say(call);
  }

  /** The host's stream was lost or went silent: main's lease says so, and keeps what it had. */
  lose(): void {
    this.#delivering = false;
    for (const call of this.followers)
      call.send({
        kind: 'control',
        control: {kind: 'stale', reason: 'silent', attempt: 1, valueAt: this.#since},
      });
  }
}

/** The same protobuf peer as the app journeys, through the Electron byte bridge role. */
class RunPeer extends FakeGatewayBridge {
  readonly host: LivenessHost;
  constructor(
    readonly input: WireFixture,
    readonly records: StoreRecord[],
    readonly loading: boolean,
  ) {
    super();
    this.host = new LivenessHost(input.run);
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
    } else if (method === this.input.methods.log) receive(new Uint8Array(this.input.log));
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
    if (!input.run.head) throw new Error('head required');
    input.run.head.epoch = epoch;
  }
  return new RunPeer(input, records, loading);
}

const NAVIGATION = {runAgain: () => undefined, openWindow: () => undefined};
const ADDRESS = {
  zoom: () => '',
  move: () => {},
  subscribe: () => () => {},
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
            problems={createProblemBoard()}
            projectRoot={recordedProjectRoot('/project')}
            seed={{
              identity: {host: 'local' as HostKey, runId: 'run-a'},
              title: {project: '/project', task: 'mpmc'},
              hostLabel: 'host-a',
              unavailable: {},
            }}
            bridge={peer}
            subscriptions={peer.host.port}
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
  expect(peer.host.followers).toEqual([]);
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
    fc.asyncProperty(
      STATE,
      LIVENESS,
      BACKGROUND,
      fc.boolean(),
      async (lifecycle, liveness, background, unknownProject) => {
        if (!unknownProject) {
          await verifyOffers({...background, lifecycle, liveness, loading: false});
          return;
        }
        await withPeer(
          peerFor({...background, lifecycle, liveness, loading: false}),
          async container => {
            expect(controlButtons(container)).toEqual([]);
            expect(container.textContent).toContain('Project unknown');
          },
          {projectRoot: undefined},
        );
      },
    ),
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
          problems={createProblemBoard()}
          projectRoot={location.projectRoot}
          seed={{
            identity: {host: 'local' as HostKey, runId: 'run-a'},
            title: {project: '/project', task: 'mpmc'},
            hostLabel: 'host-a',
            unavailable: {},
          }}
          bridge={peer}
          subscriptions={peer.host.port}
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
  expect(peer.host.followers).toEqual([]);
  expect(clock.pending).toBe(0);
}

const controlButtons = (container: HTMLElement) =>
  [...container.querySelectorAll('button')]
    .map(button => button.textContent ?? '')
    .filter(text => ['Pause', 'Pause now', 'Guidance', 'Resume'].includes(text));

const FAULT = fc.record({
  // The intent history shares the lifecycle's reader: one records stream, not two. The host's
  // liveness is main's subscription: its stream ending or stalling reaches the window as one
  // thing, the host no longer delivering.
  source: fc.constantFrom('liveness', 'lifecycle'),
  fault: fc.constantFrom('end', 'stall'),
  elapsed: fc.integer({min: 0, max: 31_000}),
});

/** The records streams the reader follows; the host's liveness is `peer.host`, held by main. */
function followers(peer: RunPeer) {
  return peer.streams.filter(stream => {
    if (stream.cancelled) return false;
    if (stream.method !== peer.input.methods.tail) return false;
    const request = fromBinary(TailRequestSchema, stream.bytes);
    return !request.sessionId && request.fromSeq > 0n;
  });
}

/** The run holds one observation of the host's runs, shared by the header and the controls. */
function assertOneLease(peer: RunPeer) {
  expect(peer.host.followers.length).toBeLessThanOrEqual(1);
}

/** The header and the controls project one reading of the writer, whichever stream led. */
function assertAgree(container: HTMLElement) {
  const buttons = controlButtons(container);
  const word = statusWord(container);
  if (buttons.some(text => ['Pause', 'Pause now', 'Guidance'].includes(text)))
    expect(['Starting', 'Running', 'Paused']).toContain(word ?? 'no status word');
  if (buttons.includes('Resume')) expect(['Starting', 'Running']).not.toContain(word ?? '');
}

function assertVisible(container: HTMLElement) {
  assertAgree(container);
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
            const old = fault.source === 'lifecycle' ? followers(peer) : [];
            await act(async () => {
              if (fault.source === 'liveness') peer.host.lose();
              else if (fault.fault === 'end')
                for (const stream of old) stream.ended('disconnected');
            });
            expect(
              controlButtons(container).some(text =>
                ['Pause', 'Pause now', 'Guidance'].includes(text),
              ),
            ).toBe(false);
            assertVisible(container);
            assertOneLease(peer);
            await act(async () => clock.advanceBy(fault.elapsed));
            expect(
              controlButtons(container).some(text =>
                ['Pause', 'Pause now', 'Guidance'].includes(text),
              ),
            ).toBe(false);
            assertVisible(container);
            assertOneLease(peer);
            await act(async () => clock.advanceBy(31_000));
            // The reader reopens a records stream that ended; main reopens the host's own.
            if (fault.source === 'lifecycle' && fault.fault === 'end')
              expect(peer.streams.length).toBeGreaterThan(before);
            assertVisible(container);
            assertOneLease(peer);
            const recovered = controlButtons(container);
            // Late words from an observation the window has released are fenced by its port.
            const released = peer.host.port.calls.filter(call => !call.open);
            await act(async () => {
              for (const call of released)
                call.send({
                  kind: 'entry',
                  entry: create(TopicEntrySchema, {
                    key: RUN_ID,
                    value: {
                      case: 'runs',
                      value: create(RunEntrySchema, {
                        head: {epoch: 1},
                        liveness: RunLiveness.ALIVE,
                      }),
                    },
                  }),
                });
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
        if (!snapshot.run.head) throw new Error('head required');
        snapshot.run.head.epoch = epoch;
        // The host has the successor, while the already-open lifecycle follower stalls.
        peer.input.skeleton = snapshot.skeleton;
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
            if (fault.source === 'liveness') peer.host.lose();
            else if (fault.fault === 'end')
              for (const stream of followers(peer)) stream.ended('disconnected');
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
          peer.host.publish(snapshot.run);
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
    for (const stream of followers(peer))
      stream.receive(
        toBinary(
          TailResponseSchema,
          create(TailResponseSchema, {records: [toBinary(RecordSchema, successor)]}),
        ),
      );
  else if (lead === 'liveness') peer.host.publish(snapshot.run);
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
              expect(container.textContent).toContain('Project unknown');
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
  // The host says what it says, or stops delivering, or delivers again.
  kind: fc.constantFrom('frame', 'lose', 'resume'),
  elapsed: fc.integer({min: 0, max: 35000}),
});

async function receiveHostStep(peer: RunPeer, step: Generated<typeof RECEIPT>) {
  await act(async () => {
    if (step.kind === 'lose') peer.host.lose();
    else peer.host.publish();
  });
}

test('property: only the host delivering decides the page controls, however long it stays silent', async () => {
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
        // The lease on the host's frames is main's: the window has none, so time alone changes nothing.
        let delivering = true;
        const check = () => {
          expect(controlButtons(container).includes('Pause')).toBe(delivering);
          assertVisible(container);
          assertOneLease(peer);
        };
        check();
        for (const step of steps) {
          await receiveHostStep(peer, step);
          delivering = step.kind !== 'lose';
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
            {kind: 'lose', elapsed: 35000},
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
    .find(text =>
      ['Starting', 'Running', 'Paused', 'Stopped', 'Failed', 'Interrupted'].includes(text),
    );
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
          let seq = peer.records.at(-1)?.seq ?? 0n;
          for (let index = 1; index <= count; index += 1) {
            const nextEpoch = epoch + index;
            const packet = (running: boolean) =>
              create(RunEntrySchema, {
                liveness: RunLiveness.ALIVE,
                head: {
                  epoch: nextEpoch,
                  committedSeq: seq + (running ? 2n : 1n),
                  lifecycle: {
                    state: running ? {case: 'running', value: {}} : {case: 'starting', value: {}},
                  },
                },
              });
            const deliver = async (entry: RunEntry) => act(async () => peer.host.publish(entry));
            await deliver(packet(false));
            expect(statusWord(container)).toBe('Starting');
            expect(controlButtons(container)).toEqual(['Pause', 'Pause now']);
            await deliver(packet(true));
            expect(statusWord(container)).toBe('Running');
            expect(controlButtons(container)).toEqual(['Pause', 'Pause now', 'Guidance']);
            await deliver(packet(false));
            expect(statusWord(container)).toBe('Running');
            // The host stops delivering: its last word is no fact, and the controls go.
            await act(async () => peer.host.lose());
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

const SUCCESSOR_FRAMES = fc.record({
  running: fc.integer({min: 0, max: 4}),
  liveness: fc.array(
    fc.constantFrom(RunLiveness.ALIVE, RunLiveness.INTERRUPTED, RunLiveness.UNSPECIFIED),
    {minLength: 1, maxLength: 5},
  ),
});

function hostFrame(
  epoch: number,
  committedSeq: bigint,
  liveness: RunLiveness,
  state: {case: 'starting' | 'running'; value: object} | {case: 'ended'; value: object},
) {
  return create(RunEntrySchema, {liveness, head: {epoch, committedSeq, lifecycle: {state}}});
}

/** A lock probe is a fact of the writer that held the lock: before this reader has seen the
 * successor's writer alive, a free lock is only the gap before it starts. */
function expectSuccessorScreen(
  container: HTMLElement,
  frame: {liveness: RunLiveness; running: boolean},
  aliveSeen: boolean,
) {
  const word = frame.running ? 'Running' : 'Starting';
  if (frame.liveness === RunLiveness.INTERRUPTED && aliveSeen) {
    expect(controlButtons(container)).toEqual(['Resume']);
    expect(statusWord(container)).not.toBe(word);
    return;
  }
  expect(statusWord(container)).toBe(word);
  const live = frame.running ? ['Pause', 'Pause now', 'Guidance'] : ['Pause', 'Pause now'];
  expect(controlButtons(container)).toEqual(frame.liveness === RunLiveness.ALIVE ? live : []);
}

test('property: a successor epoch never shows the predecessor outcome or an unconfirmed lock probe, whatever liveness its frames carry', async () => {
  await fc.assert(
    fc.asyncProperty(
      fc.integer({min: 1, max: 8}),
      fc.constantFrom(Outcome.STOPPED, Outcome.FAILED, Outcome.INTERRUPTED, Outcome.FINISHED),
      SUCCESSOR_FRAMES,
      async (epoch, outcome, {running, liveness}) => {
        const ended = {case: 'ended' as const, value: {outcome}};
        const peer = peerFor({
          epoch,
          lifecycle: create(LifecycleSchema, {state: ended}),
          liveness: RunLiveness.ENDED,
          loading: false,
          history: [],
          modernHead: true,
        });
        await withPeer(peer, async container => {
          const seq = peer.records.at(-1)?.seq ?? 0n;
          const deliver = (entry: RunEntry) => act(async () => peer.host.publish(entry));
          await deliver(hostFrame(epoch, seq, RunLiveness.ENDED, ended));
          let aliveSeen = false;
          for (const [index, value] of liveness.entries()) {
            const frame = {liveness: value, running: index >= liveness.length - running};
            aliveSeen ||= value === RunLiveness.ALIVE;
            const state = {
              case: frame.running ? ('running' as const) : ('starting' as const),
              value: {},
            };
            await deliver(hostFrame(epoch + 1, seq + BigInt(index + 1), value, state));
            expectSuccessorScreen(container, frame, aliveSeen);
          }
        });
      },
    ),
    {
      numRuns: 24,
      seed: 341811,
      examples: [
        [1, Outcome.STOPPED, {running: 0, liveness: [RunLiveness.INTERRUPTED]}],
        [
          1,
          Outcome.FAILED,
          {running: 1, liveness: [RunLiveness.UNSPECIFIED, RunLiveness.INTERRUPTED]},
        ],
      ],
    },
  );
});
