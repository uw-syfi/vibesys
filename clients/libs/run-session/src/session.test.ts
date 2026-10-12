import {describe, expect, test} from 'bun:test';
import {BackendClientError, type RunSnapshot} from '@vibesys/contracts';
import {checkMachine, type Live, type MachineAction} from '@vibesys/machine/testing';
import {type IterationNavigationState, initialIterationNavigation} from '@vibesys/run-state';
import type {Seeded} from '@vibesys/store/testing';
import type {ControlChannelState} from '@vibesys/transport';
import {initialTransition, transition} from 'xstate';
import {
  type Reachability,
  type RefreshOutcome,
  type SessionEvent,
  type SessionSnapshot,
  type SnapshotResult,
  sessionInvariants,
  sessionMachine,
  sessionStartupSettled,
} from './session.js';

const ERRORS = [new Error('snapshot failed'), new Error('socket closed'), new Error('parse')];
const OUTAGE = new BackendClientError('disconnected', 'WebSocket transport error');
const CONTROLS: readonly ControlChannelState[] = [
  {status: 'connected'},
  {status: 'disconnected', error: OUTAGE, everConnected: false, retrying: false},
  {status: 'disconnected', error: OUTAGE, everConnected: true, retrying: true},
];
const VISIBLE: Reachability = {visible: true, online: true};

/** An event the machine takes: one the shell sends, or an operation's outcome. */
type Event =
  | SessionEvent
  | {readonly type: string; readonly output?: unknown; readonly error?: unknown};

function reach(random: Seeded): Reachability {
  return {visible: random.next() < 0.8, online: random.next() < 0.8};
}

/** The snapshot a load with `issue` returned: its sequence names the load, to trace what applied. */
function snapshotOf(issue: number): RunSnapshot {
  return {run_id: 'run-1', sequence: issue, status: 'running'};
}

function loaded(issue: number): SnapshotResult {
  return {ok: true, snapshot: snapshotOf(issue)};
}

function navigation(revision: string): IterationNavigationState {
  return {...initialIterationNavigation(), runId: 'run-1', revision};
}

const REPLACED: SnapshotResult = {
  ok: false,
  error: new Error('store replaced'),
  reason: 'store-replaced',
};

function snapshotResult(random: Seeded, issue: number): SnapshotResult {
  switch (random.int(4)) {
    case 0:
      return {ok: false, error: random.pick(ERRORS)};
    case 1:
      return REPLACED;
    default:
      return loaded(issue);
  }
}

function refreshOutcome(random: Seeded, issue: number): RefreshOutcome {
  switch (random.int(4)) {
    case 0:
      return {kind: 'redial-failed', error: random.pick(ERRORS)};
    case 1:
      return {kind: 'ready'};
    default:
      return {kind: 'snapshot', result: snapshotResult(random, issue)};
  }
}

/** One outcome of each live operation, its output naming the load it answers. */
function settleOne(random: Seeded, live: Live, snapshot: SessionSnapshot): Event[] {
  const {recovery, replacement} = snapshot.context;
  return live.operations.flatMap(operation => {
    const failed = live.error(operation, random.pick(ERRORS));
    switch (operation) {
      case 'subscribe':
        return [live.done(operation), failed];
      case 'refresh':
        return [live.done(operation, refreshOutcome(random, recovery.issue)), failed];
      case 'loadReplacementSnapshot':
        return [live.done(operation, snapshotResult(random, replacement.issue)), failed];
      case 'loadIterations':
        return [live.done(operation, navigation(`revision-${random.int(4)}`)), failed];
      default:
        return [failed];
    }
  });
}

function plainEvent(random: Seeded): SessionEvent {
  switch (random.int(14)) {
    case 0:
      return {type: 'start'};
    case 1:
    case 2:
      return {type: 'stream-established', storeId: `store-${random.int(4) + 1}`};
    case 3:
      return {
        type: 'stream',
        state: random.bool()
          ? {status: 'connected'}
          : {status: 'disconnected', error: random.pick(ERRORS)},
      };
    case 4:
      return {type: 'fault', error: random.pick(ERRORS)};
    case 5:
      return {type: 'controls', state: random.pick(CONTROLS)};
    case 6:
    case 7:
      return {type: 'wake', reach: reach(random)};
    case 8:
      return {type: 'reattach', reach: reach(random), runEnded: random.bool()};
    case 9:
      return {type: 'offline', runEnded: random.bool()};
    case 10:
      return {type: 'reconnect-controls'};
    case 11:
      return {type: 'projection-rejected', error: random.pick(ERRORS)};
    case 12:
      return {type: 'iterations-refresh', storeId: `store-${random.int(4) + 1}`};
    default:
      // Rare, so most sequences stay open long enough to interleave.
      return random.int(4) === 0 ? {type: 'close'} : {type: 'start'};
  }
}

/** A custom action's params, by its type. */
function performed<P>(actions: readonly MachineAction[], type: string): P[] {
  return actions.flatMap(action => (action.type === type ? [action.params as P] : []));
}

function spawned(actions: readonly MachineAction[], id: string): boolean {
  return actions.some(
    action =>
      action.type === 'xstate.spawnChild' &&
      (action.params as {id?: string} | undefined)?.id === id,
  );
}

function appliedSnapshots(actions: readonly MachineAction[]): RunSnapshot[] {
  return performed<{snapshot: RunSnapshot | null}>(actions, 'applySnapshot').flatMap(
    ({snapshot}) => (snapshot === null ? [] : [snapshot]),
  );
}

/** The issue and the stale causes a settled load answered for, by the operation it came from. */
function answered(
  before: SessionSnapshot,
  event: Event,
): {readonly issue: number; readonly observed: number; readonly result: SnapshotResult} | null {
  const {recovery, replacement} = before.context;
  if (event.type === 'xstate.done.actor.loadReplacementSnapshot') {
    const result = (event as {output: SnapshotResult}).output;
    return {issue: replacement.issue, observed: replacement.problemEpoch, result};
  }
  if (event.type === 'xstate.done.actor.refresh') {
    const outcome = (event as {output: RefreshOutcome}).output;
    if (outcome.kind !== 'snapshot') return null;
    return {issue: recovery.issue, observed: recovery.problemEpoch, result: outcome.result};
  }
  return null;
}

/** Fencing: a snapshot is applied only by the load that carried it, issued after the newest applied. */
function fencingProperties(
  before: SessionSnapshot,
  event: Event,
  after: SessionSnapshot,
  actions: readonly MachineAction[],
): void {
  expect(after.context.applied).toBeGreaterThanOrEqual(before.context.applied);
  expect(after.context.applied).toBeLessThanOrEqual(after.context.issued);
  const applied = appliedSnapshots(actions);
  if (applied.length > 0) {
    const load = answered(before, event);
    expect(load).not.toBeNull();
    expect(applied.map(snapshot => snapshot.sequence)).toEqual([load?.issue ?? -1]);
    expect(load?.issue ?? -1).toBeGreaterThan(before.context.applied);
    expect(after.context.applied).toBe(load?.issue ?? -1);
  }
  // A proven store fences every load issued before it.
  if (spawned(actions, 'loadReplacementSnapshot')) {
    expect(after.context.applied).toBeGreaterThanOrEqual(after.context.replacement.issue - 1);
  }
  // Only an applied snapshot reevaluates the stream.
  const reevaluated = performed<{applied: boolean}>(actions, 'reevaluateStream').some(
    ({applied: yes}) => yes,
  );
  if (reevaluated) expect(applied).toHaveLength(1);
}

/** Problem epochs: monotone, and a snapshot clears only the stale causes its load saw. */
function problemProperties(before: SessionSnapshot, event: Event, after: SessionSnapshot): void {
  expect(after.context.problemEpoch).toBeGreaterThanOrEqual(before.context.problemEpoch);
  // A transient connection fault never erases stronger snapshot or projection evidence.
  const stronger =
    before.context.streamProblem === 'snapshot' || before.context.streamProblem === 'projection';
  if (stronger && ['stream', 'fault', 'offline'].includes(event.type)) {
    expect(after.context.streamProblem).toBe(before.context.streamProblem);
    expect(after.context.view.error).toBe(before.context.view.error);
  }
  const cleared = before.context.streamProblem !== 'none' && after.context.streamProblem === 'none';
  if (!cleared) return;
  const load = answered(before, event);
  if (load !== null) {
    expect(load.result.ok).toBe(true);
    expect(before.context.problemEpoch).toBeLessThanOrEqual(load.observed);
  } else {
    // Otherwise only connection proof clears a connection problem, and only over a trusted snapshot.
    expect(event).toMatchObject({type: 'stream', state: {status: 'connected'}});
    expect(before.context.streamProblem).toBe('connection');
    expect(before.context.streamSnapshot).toBe('trusted');
  }
}

/** Recovery is single-flight and keeps the strongest intent until the stream gets it. */
function recoveryProperties(
  before: SessionSnapshot,
  event: Event,
  actions: readonly MachineAction[],
): void {
  if (spawned(actions, 'refresh')) {
    // A refresh starts only when none is in flight, and only when the page can reach anything.
    expect(before.matches({open: {recovery: 'none'}})).toBe(true);
    expect(event.type === 'wake' || event.type === 'reattach').toBe(true);
    expect((event as {reach: Reachability}).reach).toEqual(VISIBLE);
  }
  const load = answered(before, event);
  const storeReplaced = load !== null && !load.result.ok && load.result.reason === 'store-replaced';
  const heldIntent = before.matches({open: {recovery: 'refreshing'}})
    ? before.context.recovery.intent
    : null;
  for (const {mode} of performed<{mode: string}>(actions, 'recoverStream')) {
    // The stream recovers with the intent the flight held when it settled: a Reattach that
    // overlapped a lifecycle retry still gets its fresh bootstrap.
    if (storeReplaced || heldIntent === 'rebootstrap') expect(mode).toBe('rebootstrap');
  }
}

function properties(
  before: SessionSnapshot,
  event: Event,
  after: SessionSnapshot,
  actions: readonly MachineAction[],
): void {
  // The snapshot on screen is never forgotten, and only a successful load puts one there.
  if (before.context.view.hasSnapshot) expect(after.context.view.hasSnapshot).toBe(true);
  else if (after.context.view.hasSnapshot) expect(answered(before, event)?.result.ok).toBe(true);
  // A closed session stays closed and does nothing.
  if (before.status === 'done') expect({after, actions}).toEqual({after: before, actions: []});
  if (event.type === 'close' && before.status === 'active') {
    expect(after.status).toBe('done');
    expect(performed(actions, 'closeShell')).toHaveLength(1);
  }
  // A healthy ended run's Reattach is inert.
  if (
    event.type === 'reattach' &&
    (event as {runEnded: boolean}).runEnded &&
    before.context.view.status !== 'stale'
  ) {
    expect(after).toBe(before);
  }
  fencingProperties(before, event, after, actions);
  problemProperties(before, event, after);
  recoveryProperties(before, event, actions);
}

type Request =
  | {readonly kind: 'subscribe'}
  | {readonly kind: 'refresh'; readonly mode: string}
  | {readonly kind: 'load-replacement-snapshot'; readonly storeId: string}
  | {readonly kind: 'load-iterations'; readonly storeId: string}
  | {readonly kind: 'publish-iterations'; readonly navigation: IterationNavigationState}
  | {readonly kind: 'apply-snapshot'; readonly snapshot: RunSnapshot}
  | {readonly kind: 'recover-stream'; readonly mode: string}
  | {readonly kind: 'reevaluate-stream'}
  | {readonly kind: 'close'};

interface ActionParams {
  readonly id?: string;
  readonly snapshot?: RunSnapshot | null;
  readonly mode?: string;
  readonly applied?: boolean;
  readonly navigation?: IterationNavigationState | null;
}

/** The operation an `xstate.spawnChild` started, as the request the shell performs. */
function started(after: SessionSnapshot, id: string | undefined): Request[] {
  switch (id) {
    case 'subscribe':
      return [{kind: 'subscribe'}];
    case 'refresh':
      return [{kind: 'refresh', mode: after.context.recovery.intent}];
    case 'loadReplacementSnapshot':
      return [{kind: 'load-replacement-snapshot', storeId: after.context.replacement.storeId}];
    case 'loadIterations':
      return [{kind: 'load-iterations', storeId: after.context.iterations.loadingStoreId}];
    default:
      return [];
  }
}

/** What one step asked the shell for, in the order it asked. */
function requestsOf(after: SessionSnapshot, actions: readonly MachineAction[]): Request[] {
  return actions.flatMap((action): Request[] => {
    const params = (action.params ?? {}) as ActionParams;
    switch (action.type) {
      case 'xstate.spawnChild':
        return started(after, params.id);
      case 'applySnapshot':
        return params.snapshot ? [{kind: 'apply-snapshot', snapshot: params.snapshot}] : [];
      case 'recoverStream':
        return [{kind: 'recover-stream', mode: params.mode ?? ''}];
      case 'reevaluateStream':
        return params.applied === true ? [{kind: 'reevaluate-stream'}] : [];
      case 'publishIterations':
        return params.navigation === null || params.navigation === undefined
          ? []
          : [{kind: 'publish-iterations', navigation: params.navigation}];
      case 'closeShell':
        return [{kind: 'close'}];
      default:
        return [];
    }
  });
}

function step(
  snapshot: SessionSnapshot,
  event: Event,
): {readonly state: SessionSnapshot; readonly requests: Request[]} {
  const [state, actions] = transition(sessionMachine, snapshot, event as SessionEvent);
  return {state, requests: requestsOf(state, actions as readonly MachineAction[])};
}

function steps(
  events: readonly Event[],
  from = initialTransition(sessionMachine)[0],
): SessionSnapshot {
  return events.reduce((snapshot, event) => step(snapshot, event).state, from);
}

const subscribed: Event = {type: 'xstate.done.actor.subscribe'};

function replacementDone(result: SnapshotResult): Event {
  return {type: 'xstate.done.actor.loadReplacementSnapshot', output: result};
}

function refreshDone(outcome: RefreshOutcome): Event {
  return {type: 'xstate.done.actor.refresh', output: outcome};
}

function iterationsDone(outcome: IterationNavigationState | null): Event {
  return {type: 'xstate.done.actor.loadIterations', output: outcome};
}

describe('session machine', () => {
  test('model: holds its invariants, fences snapshots, and orders stale causes', () => {
    checkMachine(
      {
        machine: sessionMachine,
        input: undefined,
        // Settle a live operation about half the time, so the replies of overlapping flights
        // race each other often.
        event: (random, live, snapshot) => {
          const settle = settleOne(random, live, snapshot);
          return settle.length > 0 && random.bool() ? random.pick(settle) : plainEvent(random);
        },
        invariants: sessionInvariants,
        also: (before, event, after, actions) => properties(before, event, after, actions),
      },
      60,
    );
  });

  test('starts the stream, then applies only a snapshot fenced to its store', () => {
    const asked = step(initialTransition(sessionMachine)[0], {type: 'start'});
    expect(asked.requests).toEqual([{kind: 'subscribe'}]);
    const established = step(asked.state, {type: 'stream-established', storeId: 'store-a'});
    expect(established.requests).toEqual([{kind: 'load-replacement-snapshot', storeId: 'store-a'}]);
    const handshake = step(established.state, subscribed);
    const live = step(handshake.state, replacementDone(loaded(1)));
    expect(live.requests).toEqual([{kind: 'apply-snapshot', snapshot: snapshotOf(1)}]);
    expect(live.state.context.view.hasSnapshot).toBe(true);
    expect(live.state.context.view.status).toBe('connected');
    expect(live.state.matches({open: {startup: 'done'}})).toBe(true);
  });

  test('coalesces iteration invalidations and publishes only the authoritative follow-up', () => {
    const first = step(initialTransition(sessionMachine)[0], {
      type: 'iterations-refresh',
      storeId: 'store-a',
    });
    expect(first.requests).toEqual([{kind: 'load-iterations', storeId: 'store-a'}]);

    const queued = step(first.state, {type: 'iterations-refresh', storeId: 'store-b'});
    expect(queued.requests).toEqual([]);
    expect(queued.state.context.iterations.pendingStoreId).toBe('store-b');

    const stale = step(queued.state, iterationsDone(navigation('stale')));
    expect(stale.requests).toEqual([{kind: 'load-iterations', storeId: 'store-b'}]);

    const current = navigation('current');
    const published = step(stale.state, iterationsDone(current));
    expect(published.requests).toEqual([{kind: 'publish-iterations', navigation: current}]);
    expect(published.state.matches({open: {iterations: 'ready'}})).toBe(true);
  });

  test('suppresses a superseded iteration failure and retires late output after close', () => {
    const first = step(initialTransition(sessionMachine)[0], {
      type: 'iterations-refresh',
      storeId: 'store-a',
    });
    const queued = step(first.state, {type: 'iterations-refresh', storeId: 'store-b'});
    const supersededFailure = step(queued.state, {
      type: 'xstate.error.actor.loadIterations',
      error: new Error('stale failure'),
    });
    expect(supersededFailure.requests).toEqual([{kind: 'load-iterations', storeId: 'store-b'}]);
    expect(supersededFailure.state.context.view.error).toBeNull();

    const closed = step(supersededFailure.state, {type: 'close'});
    expect(closed.requests).toEqual([{kind: 'close'}]);
    const late = step(closed.state, iterationsDone(navigation('late')));
    expect(late).toEqual({state: closed.state, requests: []});
  });

  test('start-up settles only after the initial iteration query settles', () => {
    let state = steps([
      {type: 'start'},
      {type: 'stream-established', storeId: 'store-a'},
      {type: 'iterations-refresh', storeId: 'store-a'},
      subscribed,
      replacementDone(loaded(1)),
    ]);
    expect(state.matches({open: {startup: 'done', iterations: 'loading'}})).toBe(true);
    expect(sessionStartupSettled(state)).toBe(false);

    state = step(state, iterationsDone(navigation('initial'))).state;
    expect(sessionStartupSettled(state)).toBe(true);
  });

  test('an iteration fault leaves a healthy stream connected and a successful retry clears it', () => {
    const iterationFailure = new Error('iteration RPC failed');
    let state = steps([
      {type: 'start'},
      {type: 'stream-established', storeId: 'store-a'},
      {type: 'iterations-refresh', storeId: 'store-a'},
      subscribed,
      replacementDone(loaded(1)),
      iterationsDone(navigation('initial')),
    ]);
    expect(state.context.view).toMatchObject({
      status: 'connected',
      error: null,
      iterationError: null,
    });

    state = steps(
      [
        {type: 'iterations-refresh', storeId: 'store-a'},
        {type: 'xstate.error.actor.loadIterations', error: iterationFailure},
      ],
      state,
    );
    expect(state.context.streamConnected).toBe(true);
    expect(state.context.view).toMatchObject({
      status: 'connected',
      error: null,
      iterationError: iterationFailure,
    });

    state = steps(
      [{type: 'iterations-refresh', storeId: 'store-a'}, iterationsDone(navigation('recovered'))],
      state,
    );
    expect(state.context.view).toMatchObject({
      status: 'connected',
      error: null,
      iterationError: null,
    });
  });

  test('a failed start-up snapshot keeps the page stale after the stream subscribes', () => {
    const failure = new Error('no snapshot');
    const state = steps([
      {type: 'start'},
      {type: 'stream-established', storeId: 'store-a'},
      subscribed,
      replacementDone({ok: false, error: failure}),
    ]);
    expect(state.context.view).toMatchObject({status: 'stale', error: failure, hasSnapshot: false});
    expect(state.matches({open: {startup: 'done'}})).toBe(true);
  });

  test('a Reattach during a lifecycle refresh promotes the stream recovery', () => {
    const state = steps([{type: 'wake', reach: VISIBLE}]);
    const reattach = step(state, {type: 'reattach', reach: VISIBLE, runEnded: false});
    expect(reattach.requests).toEqual([]);
    const settled = step(reattach.state, refreshDone({kind: 'snapshot', result: loaded(1)}));
    expect(settled.requests).toEqual([{kind: 'recover-stream', mode: 'rebootstrap'}]);
  });

  test('a failed fresh stream releases recovery for a later Reattach', () => {
    const failure = ERRORS[1] ?? new Error();
    const reports: readonly Event[] = [
      {type: 'stream', state: {status: 'disconnected', error: failure}},
      {type: 'fault', error: failure},
    ];
    for (const report of reports) {
      const state = steps([
        {type: 'start'},
        {type: 'stream-established', storeId: 'store-a'},
        replacementDone(loaded(1)),
        subscribed,
      ]);
      const first = step(state, {type: 'reattach', reach: VISIBLE, runEnded: false});
      expect(first.requests).toEqual([{kind: 'refresh', mode: 'rebootstrap'}]);
      const establishing = step(first.state, refreshDone({kind: 'ready'}));
      expect(establishing.requests).toEqual([{kind: 'recover-stream', mode: 'rebootstrap'}]);

      const failed = step(establishing.state, report);
      const retry = step(failed.state, {type: 'reattach', reach: VISIBLE, runEnded: false});
      expect(retry.requests).toEqual([{kind: 'refresh', mode: 'rebootstrap'}]);
    }
  });

  test('fences pre-replacement replies and accepts only the proven generation snapshot', () => {
    const refreshing = steps([{type: 'wake', reach: VISIBLE}]);
    const replaced = step(refreshing, {type: 'stream-established', storeId: 'store-b'});
    // Proving the store retires the refresh in flight: its reply could only describe the old one.
    expect(replaced.requests).toEqual([{kind: 'load-replacement-snapshot', storeId: 'store-b'}]);
    expect(replaced.state.matches({open: {recovery: {refreshing: 'snapshot'}}})).toBe(true);
    const staleRefresh = step(replaced.state, refreshDone({kind: 'snapshot', result: loaded(1)}));
    expect(staleRefresh).toEqual({state: replaced.state, requests: []});

    const accepted = step(staleRefresh.state, replacementDone(loaded(2)));
    expect(accepted.requests).toEqual([
      {kind: 'apply-snapshot', snapshot: snapshotOf(2)},
      {kind: 'reevaluate-stream'},
    ]);
    expect(accepted.state.matches({open: {recovery: 'none'}})).toBe(true);
  });

  test('keeps a rejected stream projection stale until newer snapshot proof succeeds', () => {
    let state = steps([
      {type: 'start'},
      {type: 'stream-established', storeId: 'store-a'},
      subscribed,
      replacementDone(loaded(1)),
    ]);
    expect(state.context.view.status).toBe('connected');

    const rejection = new Error('event stream crossed run identities');
    state = steps(
      [
        {type: 'projection-rejected', error: rejection},
        {type: 'stream', state: {status: 'connected'}},
      ],
      state,
    );
    expect(state.context.view).toMatchObject({status: 'stale', error: rejection});

    state = steps(
      [
        {type: 'stream', state: {status: 'disconnected', error: new Error('socket closed')}},
        {type: 'stream', state: {status: 'connected'}},
      ],
      state,
    );
    expect(state.context.view).toMatchObject({status: 'stale', error: rejection});

    state = steps(
      [{type: 'wake', reach: VISIBLE}, refreshDone({kind: 'snapshot', result: loaded(2)})],
      state,
    );
    expect(state.context.view).toMatchObject({status: 'connected', error: null});
  });

  test('a load rejected as another run keeps start-up stale without a snapshot', () => {
    const foreign = new Error('Ignored data for run run-b; this projection owns run-a');
    const state = steps([
      {type: 'start'},
      {type: 'stream-established', storeId: 'store-a'},
      subscribed,
      {type: 'stream', state: {status: 'connected'}},
      replacementDone({ok: false, error: foreign}),
      // A live socket does not retire the failed proof.
      {type: 'stream', state: {status: 'connected'}},
    ]);
    expect(state.context.view).toMatchObject({status: 'stale', error: foreign, hasSnapshot: false});
    expect(state.matches({open: {startup: 'done'}})).toBe(true);
  });

  test('a superseded fenced snapshot keeps recovery owned until a fresh store is established', () => {
    const established = steps([
      {type: 'wake', reach: VISIBLE},
      {type: 'stream-established', storeId: 'store-b'},
    ]);
    const superseded = step(established, replacementDone(REPLACED));
    expect(superseded.requests).toEqual([{kind: 'recover-stream', mode: 'rebootstrap'}]);
    expect(superseded.state.matches({open: {recovery: {refreshing: 'establishing'}}})).toBe(true);
    expect(superseded.state.context.recovery.intent).toBe('rebootstrap');
    expect(superseded.state.matches({open: {replacement: 'none'}})).toBe(true);
  });

  test('a snapshot that began before a stream fault does not clear it', () => {
    const fault = new Error('replacement stream sent an invalid frame');
    const state = steps([
      {type: 'start'},
      {type: 'stream-established', storeId: 'store-a'},
      subscribed,
      {type: 'stream', state: {status: 'connected'}},
      {type: 'fault', error: fault},
      replacementDone(loaded(1)),
    ]);
    expect(state.context.view).toMatchObject({hasSnapshot: true, status: 'stale', error: fault});
  });

  test('a later load fences an earlier one whatever order their replies arrive in', () => {
    const twoLoads = steps([
      {type: 'start'},
      {type: 'stream-established', storeId: 'store-a'},
      subscribed,
      replacementDone(loaded(1)),
      {type: 'wake', reach: VISIBLE},
    ]);
    // The refresh's load is issue 2; a replacement batch then proves store-b and issues 3.
    const replaced = step(twoLoads, {type: 'stream-established', storeId: 'store-b'});
    expect(replaced.state.context.applied).toBe(2);
    const applied = step(replaced.state, replacementDone(loaded(3)));
    expect(applied.requests[0]).toEqual({kind: 'apply-snapshot', snapshot: snapshotOf(3)});
    expect(applied.state.context.applied).toBe(3);
  });

  test('regression: a late start-up snapshot never overwrites a newer refresh snapshot', () => {
    const racing = steps([
      {type: 'start'},
      {type: 'stream-established', storeId: 'store-a'},
      subscribed,
      {type: 'wake', reach: VISIBLE},
      refreshDone({kind: 'snapshot', result: loaded(2)}),
    ]);
    expect(racing.context.applied).toBe(2);
    const late = step(racing, replacementDone(loaded(1)));
    expect(late.requests).toEqual([]);
    // The start-up flight still settles: its handshake and a usable snapshot both happened.
    expect(late.state.matches({open: {startup: 'done', replacement: 'none'}})).toBe(true);
  });

  test('every operation that fails ends its flight instead of leaving its region waiting', () => {
    const failure = new Error('operation rejected');
    const subscribing = steps([{type: 'start'}]);
    const subscribeFailed = step(subscribing, {
      type: 'xstate.error.actor.subscribe',
      error: failure,
    });
    expect(subscribeFailed.state.matches({open: {startup: 'done'}})).toBe(true);
    expect(subscribeFailed.state.context.view).toMatchObject({status: 'stale', error: failure});

    const refreshing = steps([{type: 'wake', reach: VISIBLE}]);
    const refreshFailed = step(refreshing, {type: 'xstate.error.actor.refresh', error: failure});
    expect(refreshFailed.state.matches({open: {recovery: 'none'}})).toBe(true);
    expect(refreshFailed.state.context.view).toMatchObject({status: 'stale', error: failure});

    const loading = steps([
      {type: 'start'},
      {type: 'stream-established', storeId: 'store-a'},
      subscribed,
    ]);
    const loadFailed = step(loading, {
      type: 'xstate.error.actor.loadReplacementSnapshot',
      error: failure,
    });
    expect(loadFailed.state.matches({open: {replacement: 'none', startup: 'done'}})).toBe(true);
    expect(loadFailed.state.context.view).toMatchObject({status: 'stale', error: failure});

    const iterationLoading = steps([{type: 'iterations-refresh', storeId: 'store-a'}]);
    const iterationFailed = step(iterationLoading, {
      type: 'xstate.error.actor.loadIterations',
      error: failure,
    });
    expect(iterationFailed.state.matches({open: {iterations: 'ready'}})).toBe(true);
    expect(iterationFailed.state.context.view).toMatchObject({
      status: 'connecting',
      error: null,
      iterationError: failure,
    });
  });
});
