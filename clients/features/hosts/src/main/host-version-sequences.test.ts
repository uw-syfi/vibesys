/**
 * The host version check under every interleaving of its events, through the production wiring:
 * `Hosts` over a real `ConnectionManager` and `FakeLink`, and the real registry codec over a
 * transport whose probe replies are held until the test releases them. A generated sequence connects,
 * drops and reconnects the link, presses Retry, publishes listings, and answers any probe, current
 * or from an earlier connection generation, in any order, with a verdict (current, too old,
 * unsupported) or a failure (an ordinary host failure, or the gateway closing). After every step the
 * screen must be what the invariants of the version check say:
 *
 * - a probe answer from an earlier connection generation changes nothing (the screen and the verdict);
 * - Retry and every reconnect start a new probe and abort the old ones;
 * - a host is withheld only while the current generation's verdict says it is out of date, and a
 *   failed probe never withholds one;
 * - the latest listing is on the screen whenever the host is not withheld, including one published
 *   while it was.
 */
import {describe, expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {ConnectionManager, registrySetup} from '@vibesys/connection';
import {FakeLink, HeldReply, phaseReached, record} from '@vibesys/connection/testing';
import {type HostId, type HostListing, hostKey} from '@vibesys/hosts';
import {
  CandidateState,
  FailureCategory,
  InspectCandidateResponseSchema,
  ReadRequestSchema,
  ReadResponseSchema,
} from '@vibesys/proto';
import {SettingsStore, withHostSettings} from '@vibesys/settings';
import {FakeSettingsFilesystem} from '@vibesys/settings/testing';
import {Seeded, seedsAtDepth} from '@vibesys/store/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {Hosts} from './index.js';

const ALPHA: HostId = {kind: 'ssh', alias: 'alpha'};
const KEY = hostKey(ALPHA);
const HASH = 'ab'.repeat(32);

type Answer = 'current' | 'too_old' | 'unsupported' | 'internal' | 'closed';
const ANSWERS: readonly Answer[] = ['current', 'too_old', 'unsupported', 'internal', 'closed'];

/** Whether an answer says the checkout is out of date; the others are no verdict at all. */
const outOfDate = (answer: Answer): boolean => answer === 'too_old' || answer === 'unsupported';

interface ProbeRequest {
  readonly requestId: string;
  readonly path: string;
  readonly selectionRevision: bigint;
}

/** One probe the registry client sent, and its reply held until the test releases it. */
class HeldProbe extends HeldReply<Uint8Array> {
  readonly #request: ProbeRequest;
  constructor(signal: AbortSignal, request: ProbeRequest) {
    super(signal);
    this.#request = request;
  }

  respond(answer: Answer): void {
    this.answered = true;
    if (answer === 'closed') {
      this.fail(new Error('gateway closed'));
      return;
    }
    const base = {...this.#request, schemaHash: HASH};
    const inspection =
      answer === 'current'
        ? create(InspectCandidateResponseSchema, {
            ...base,
            state: CandidateState.FOUND,
            minimumVersion: '0.1.1',
            vibesysCheckout: true,
            identity: {
              root: this.#request.path,
              version: '0.1.1',
              commit: 'a'.repeat(40),
              compatible: true,
              vibesysProject: true,
              gitRepository: true,
            },
          })
        : create(InspectCandidateResponseSchema, {
            ...base,
            state: CandidateState.TOO_OLD,
            minimumVersion: '0.1.1',
            identity: {root: this.#request.path, version: '0.0.9', compatible: false},
          });
    const result =
      answer === 'current' || answer === 'too_old'
        ? ({case: 'inspectCandidate', value: inspection} as const)
        : ({
            case: 'failure',
            value: {
              category:
                answer === 'unsupported' ? FailureCategory.UNSUPPORTED : FailureCategory.INTERNAL,
              message: 'the host said so',
            },
          } as const);
    super.answer(toBinary(ReadResponseSchema, create(ReadResponseSchema, {result})));
  }
}

/** The registry's unary port: every inspect-candidate is held. */
class HeldProbes {
  readonly probes: HeldProbe[] = [];
  #waiting: (() => void)[] = [];

  /** Resolves once the n-th probe (1-based) has been sent. */
  async sent(n: number): Promise<void> {
    while (this.probes.length < n) await new Promise<void>(resolve => this.#waiting.push(resolve));
  }

  readonly unary = async (_host: HostId, bytes: Uint8Array, signal: AbortSignal) => {
    const method = fromBinary(ReadRequestSchema, bytes).method;
    if (method.case !== 'inspectCandidate') throw new Error(method.case ?? 'empty');
    const probe = new HeldProbe(signal, {
      requestId: method.value.requestId,
      path: method.value.path,
      selectionRevision: method.value.selectionRevision,
    });
    this.probes.push(probe);
    for (const wake of this.#waiting.splice(0)) wake();
    return probe.reply;
  };
}

/** What the screen shows of the host: the last listing pushed to it. */
const shows = (screen: HostListing | undefined) =>
  screen === undefined
    ? 'nothing'
    : screen.kind === 'unavailable'
      ? screen.connection.kind === 'incompatible'
        ? 'incompatible'
        : 'unavailable'
      : `listed ${screen.live.length}`;

interface Rig {
  readonly pushed: HostListing[];
  readonly probes: HeldProbe[];
  readonly link: FakeLink;
  open(): Promise<void>;
  quiesce(): Promise<void>;
  drop(): Promise<void>;
  retry(): Promise<void>;
  publish(): Promise<void>;
  close(): Promise<void>;
}

async function rigOver(): Promise<Rig> {
  const settings = new SettingsStore({
    settingsPath: '/settings/hosts.json',
    recentPath: '/settings/recent.json',
    preferencesPath: '/settings/preferences.json',
    filesystem: new FakeSettingsFilesystem(),
    log: () => {},
  });
  await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
  const link = new FakeLink();
  const clock = new FakeClock();
  const manager = new ConnectionManager({
    linkFor: () => link,
    appVersion: 'test',
    scheduleTimeout: clock.scheduleTimeout,
    config: {delaysMs: [1], deadlineMs: 60_000, heartbeatMs: 15_000},
    log: () => {},
  });
  const held = new HeldProbes();
  const pushed: HostListing[] = [];
  const hosts = new Hosts({
    manager,
    settings,
    localCheckout: '/bundled',
    sshConfigPath: '/missing',
    transportV2: async () => true,
    setup: registrySetup({unary: held.unary}),
    runs: {start: () => {}, cancel: () => {}},
    log: () => {},
    push: listing => pushed.push(listing),
    retire: () => {},
  });
  const connection = () => {
    const open = manager.existing(KEY);
    if (open === null) throw new Error('the host has no connection');
    return open;
  };
  let published = 0;
  const rig: Rig = {
    pushed,
    probes: held.probes,
    link,
    async open() {
      await hosts.configured();
      await held.sent(1);
      await rig.quiesce();
    },
    /** A control round trip orders after everything the helper pushed; then the microtasks settle. */
    async quiesce() {
      await connection().request({type: 'ping'});
      for (let turn = 0; turn < 20; turn += 1) await Promise.resolve();
    },
    async drop() {
      const count = held.probes.length;
      const dropped = connection();
      link.helper.drop();
      await phaseReached(dropped, 'reconnecting');
      // A reconnect may wait out a backoff on the fake clock; fire it when it is armed.
      const ready = phaseReached(dropped, 'ready').then(() => true);
      while (!(await Promise.race([ready, clock.waitUntilScheduled(1).then(() => false)]))) {
        clock.runPending();
      }
      await held.sent(count + 1);

      await rig.quiesce();
    },
    async retry() {
      await hosts.signIn(ALPHA);
      await rig.quiesce();
    },
    async publish() {
      published += 1;
      link.helper.publish(record(String(published).padStart(12, '0'), `run-${published}`));
      await rig.quiesce();
    },
    async close() {
      await manager[Symbol.asyncDispose]();
    },
  };
  return rig;
}

/** What the version check has decided so far, written from its invariants and not from its code. */
class Expectation {
  withheld = false;
  /** The connection dropped since the withholding verdict, so the screen may show the reconnect. */
  #reconnected = false;
  live = 0;

  dropped(): void {
    this.#reconnected = this.withheld;
  }

  verdict(answer: Answer): void {
    this.withheld = outOfDate(answer);
    this.#reconnected = false;
  }

  /** The screens the host may show now. */
  screens(): readonly string[] {
    if (!this.withheld) return [`listed ${this.live}`];
    return this.#reconnected ? ['incompatible', 'unavailable'] : ['incompatible'];
  }
}

type Step =
  | {readonly kind: 'drop'}
  | {readonly kind: 'retry'}
  | {readonly kind: 'publish'}
  | {readonly kind: 'answer'; readonly probe: number; readonly answer: Answer};

/** A connection that keeps dropping goes offline, which is another state; two drops stay connected. */
const MOST_DROPS = 2;

function stepsFor(seed: number): Step[] {
  const random = new Seeded(seed);
  const steps: Step[] = [];
  let drops = 0;
  for (let index = 0; index < 4 + random.int(9); index += 1) {
    const roll = random.int(10);
    if (roll < 2 && drops < MOST_DROPS) {
      drops += 1;
      steps.push({kind: 'drop'});
    } else if (roll < 4) {
      steps.push({kind: 'retry'});
    } else if (roll < 6) {
      steps.push({kind: 'publish'});
    } else {
      steps.push({kind: 'answer', probe: random.int(1_000), answer: random.pick(ANSWERS)});
    }
  }
  return steps;
}

/** A drop or a Retry starts exactly one new probe and fences every earlier one. */
async function restart(rig: Rig, step: Step & {kind: 'drop' | 'retry'}): Promise<void> {
  const before = rig.probes.length;
  await (step.kind === 'drop' ? rig.drop() : rig.retry());
  expect(rig.probes.length).toBe(before + 1);
  const earlier = rig.probes.slice(0, -1);
  expect(earlier.every(probe => probe.answered || probe.signal.aborted)).toBe(true);
  expect(rig.probes.at(-1)?.signal.aborted).toBe(false);
}

/** Answer one still-open probe, current or stale; a stale answer must not reach the screen. */
async function answer(
  rig: Rig,
  expectation: Expectation,
  step: Step & {kind: 'answer'},
): Promise<void> {
  const open = rig.probes.filter(probe => !probe.answered);
  const target = open[step.probe % open.length];
  if (target === undefined) return;
  const pushes = rig.pushed.length;
  target.respond(step.answer);
  await rig.quiesce();
  if (target === rig.probes.at(-1)) expectation.verdict(step.answer);
  else expect(rig.pushed.length).toBe(pushes);
}

async function run(seed: number): Promise<void> {
  const rig = await rigOver();
  try {
    await rig.open();
    const expectation = new Expectation();
    expect(expectation.screens()).toContain(shows(rig.pushed.at(-1)));
    for (const step of stepsFor(seed)) {
      if (step.kind === 'publish') {
        expectation.live += 1;
        await rig.publish();
      } else if (step.kind === 'answer') {
        await answer(rig, expectation, step);
      } else {
        await restart(rig, step);
        if (step.kind === 'drop') expectation.dropped();
      }
      const screen = shows(rig.pushed.at(-1));
      if (!expectation.screens().includes(screen)) {
        throw new Error(
          `after ${JSON.stringify(step)}: the screen shows ${screen}, expected ${expectation.screens().join(' or ')}`,
        );
      }
    }
  } catch (error) {
    throw new Error(`seed ${seed}`, {cause: error});
  } finally {
    await rig.close();
  }
}

describe('the host version check under generated interleavings', () => {
  test('the screen follows the current generation across connects, drops, retries and late probe answers', async () => {
    for (const seed of seedsAtDepth(Array.from({length: 40}, (_, index) => index + 1)))
      await run(seed);
  });
});
