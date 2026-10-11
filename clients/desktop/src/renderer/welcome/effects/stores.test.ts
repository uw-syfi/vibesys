import {describe, expect, test} from 'bun:test';
import type {Store} from '@vibesys/core-state';
import type {HostKey} from '../../../common/hosts.js';
import type {ResumeRunId} from '../../../common/resume-run.js';
import type {
  ChromeState,
  HostListing,
  PathCompletion,
  StartOutcome,
  WelcomeBridge,
  WelcomeEvent,
  WelcomeHost,
  WelcomeOverview,
  WelcomeRecent,
  WelcomeResult,
} from '../../../common/welcome-protocol.js';
import type {HostAction, HostState} from '../state/host.js';
import {loadWelcomeStores} from './stores.js';

class Deferred<T> {
  readonly promise: Promise<T>;
  #resolve: (value: T) => void = () => undefined;

  constructor() {
    this.promise = new Promise(resolve => {
      this.#resolve = resolve;
    });
  }

  resolve(value: T): void {
    this.#resolve(value);
  }
}

class FakeWelcomeBridge implements WelcomeBridge {
  readonly platform = 'linux';
  readonly overviewReply = new Deferred<WelcomeResult<WelcomeOverview>>();
  readonly hostCalls: HostKey[] = [];
  readonly checkoutCalls: Array<{
    readonly host: HostKey;
    readonly path: string;
    readonly change: number;
    readonly reply: Deferred<WelcomeResult<string>>;
  }> = [];
  readonly cancelledCheckouts: number[] = [];
  checkout = '/vibesys';
  readonly #chrome: Array<(event: WelcomeEvent<ChromeState>) => void> = [];
  readonly #recent: Array<(event: WelcomeEvent<readonly WelcomeRecent[]>) => void> = [];
  readonly #showHost: Array<(event: WelcomeEvent<HostKey>) => void> = [];
  readonly #listing: Array<(event: WelcomeEvent<HostListing>) => void> = [];
  #sequence = 0;

  overview(): Promise<WelcomeResult<WelcomeOverview>> {
    return this.overviewReply.promise;
  }

  async hosts(): Promise<WelcomeResult<string[]>> {
    return {ok: true, value: []};
  }

  async host(host: HostKey): Promise<WelcomeResult<WelcomeHost>> {
    this.hostCalls.push(host);
    return {ok: true, value: {key: host, label: host, checkout: this.checkout}};
  }

  setCheckout(host: HostKey, path: string, change: number): Promise<WelcomeResult<string>> {
    const reply = new Deferred<WelcomeResult<string>>();
    this.checkoutCalls.push({host, path, change, reply});
    return reply.promise;
  }

  async cancelCheckout(change: number): Promise<WelcomeResult<null>> {
    this.cancelledCheckouts.push(change);
    return {ok: true, value: null};
  }

  async signIn(_host: HostKey): Promise<WelcomeResult<null>> {
    return {ok: true, value: null};
  }

  async tasks(_host: HostKey, _project: string): Promise<WelcomeResult<string[]>> {
    return {ok: true, value: []};
  }

  async complete(_host: HostKey, directory: string): Promise<WelcomeResult<PathCompletion>> {
    return {ok: true, value: {directory, entries: [], truncated: false}};
  }

  async attach(_host: HostKey, _instance: string): Promise<WelcomeResult<null>> {
    return {ok: true, value: null};
  }

  async start(
    _host: HostKey,
    _project: string,
    _task: string,
    _args: string,
    _start: number,
  ): Promise<WelcomeResult<StartOutcome>> {
    return {ok: true, value: 'started'};
  }

  async cancelStart(_start: number): Promise<WelcomeResult<null>> {
    return {ok: true, value: null};
  }

  async resume(_host: HostKey, _project: string, _run: ResumeRunId): Promise<WelcomeResult<null>> {
    return {ok: true, value: null};
  }

  async showRun(): Promise<WelcomeResult<null>> {
    return {ok: true, value: null};
  }

  async showWelcome(): Promise<WelcomeResult<null>> {
    return {ok: true, value: null};
  }

  async retry(): Promise<WelcomeResult<null>> {
    return {ok: true, value: null};
  }

  async stop(_host: HostKey, _instance: string, _force: boolean): Promise<WelcomeResult<null>> {
    return {ok: true, value: null};
  }

  onChrome(listener: (event: WelcomeEvent<ChromeState>) => void): void {
    this.#chrome.push(listener);
  }

  onRecent(listener: (event: WelcomeEvent<readonly WelcomeRecent[]>) => void): void {
    this.#recent.push(listener);
  }

  onShowHost(listener: (event: WelcomeEvent<HostKey>) => void): void {
    this.#showHost.push(listener);
  }

  onListing(listener: (event: WelcomeEvent<HostListing>) => void): void {
    this.#listing.push(listener);
  }

  listenerCounts(): number[] {
    return [this.#chrome.length, this.#recent.length, this.#listing.length, this.#showHost.length];
  }

  emitChrome(value: ChromeState): void {
    const event = this.#event(value);
    for (const listener of this.#chrome) listener(event);
  }

  emitRecent(value: readonly WelcomeRecent[]): void {
    const event = this.#event(value);
    for (const listener of this.#recent) listener(event);
  }

  emitShowHost(value: HostKey): void {
    const event = this.#event(value);
    for (const listener of this.#showHost) listener(event);
  }

  emitListing(value: HostListing): void {
    const event = this.#event(value);
    for (const listener of this.#listing) listener(event);
  }

  cursor(): number {
    return this.#sequence;
  }

  #event<T>(value: T): WelcomeEvent<T> {
    return {sequence: ++this.#sequence, value};
  }
}

describe('loadWelcomeStores', () => {
  test('cancelling an in-flight checkout revokes the same main-process operation', async () => {
    const bridge = new FakeWelcomeBridge();
    const loading = loadWelcomeStores(bridge, () => true);
    bridge.overviewReply.resolve({
      ok: true,
      value: {
        eventCursor: 0,
        settingsProblem: null,
        chrome: {mode: 'welcome', attached: null, stops: {}},
        recent: [],
        listings: [],
        host: 'local',
      },
    });
    const loaded = await loading;
    if (!loaded.ok) throw new Error(loaded.error);
    await hostReady(loaded.value.host);

    loaded.value.host.dispatch({type: 'edit-checkout'});
    loaded.value.host.dispatch({type: 'checkout-input', value: '/new'});
    loaded.value.host.dispatch({type: 'submit-checkout'});
    const call = bridge.checkoutCalls[0];
    if (call === undefined) throw new Error('checkout call expected');
    expect(call).toMatchObject({host: 'local', path: '/new'});

    loaded.value.host.dispatch({type: 'cancel-checkout'});
    expect(bridge.cancelledCheckouts).toEqual([call.change]);
    expect(loaded.value.host.getState().panel).toMatchObject({
      kind: 'ready',
      data: {checkout: '/vibesys'},
      checkout: null,
    });

    // Cancellation can lose the main process's atomic commit race. Its successful original reply
    // must update the closed panel to the durable checkout rather than leave renderer and disk split.
    const committed = new Promise<void>(resolve => {
      const unsubscribe = loaded.value.host.subscribe(() => {
        const panel = loaded.value.host.getState().panel;
        if (panel.kind !== 'ready' || panel.data.checkout !== '/new') return;
        unsubscribe();
        resolve();
      });
    });
    bridge.checkout = '/new';
    call.reply.resolve({ok: true, value: '/new'});
    await committed;
    expect(loaded.value.host.getState().panel).toMatchObject({
      kind: 'ready',
      data: {checkout: '/new'},
      checkout: null,
    });
  });

  test('keeps every push that crosses the overview and continues live', async () => {
    const bridge = new FakeWelcomeBridge();
    const loading = loadWelcomeStores(bridge, () => true);

    expect(bridge.listenerCounts()).toEqual([1, 1, 1, 1]);
    bridge.emitChrome(chrome('covered'));
    bridge.emitRecent([recent('covered')]);
    bridge.emitShowHost('ssh:covered');
    const eventCursor = bridge.cursor();
    bridge.overviewReply.resolve({
      ok: true,
      value: {
        eventCursor,
        settingsProblem: null,
        chrome: chrome('snapshot'),
        recent: [recent('snapshot')],
        listings: [],
        host: 'local',
      },
    });
    // These messages are newer than the snapshot, but arrive before its promise continuation can
    // build the stores. They must be buffered rather than disappearing into the bootstrap gap.
    bridge.emitChrome(chrome('newer'));
    bridge.emitRecent([recent('newer')]);
    bridge.emitListing(listing('newer'));
    bridge.emitShowHost('ssh:newer');

    const loaded = await loading;
    if (!loaded.ok) throw new Error(loaded.error);
    expect(loaded.value.welcome.getState().chrome.attached?.runId).toBe('newer');
    expect(loaded.value.lists.getState()).toMatchObject({
      runs: [{runId: 'newer'}],
      listings: {'ssh:newer': {host: 'ssh:newer', live: [{runId: 'newer'}]}},
    });
    expect(loaded.value.host.getState().host).toBe('ssh:newer');

    bridge.emitChrome(chrome('live'));
    bridge.emitRecent([recent('live')]);
    expect(loaded.value.welcome.getState().chrome.attached?.runId).toBe('live');
    expect(loaded.value.lists.getState().runs[0]?.runId).toBe('live');
  });

  test('converges for every snapshot position in an ordered push sequence', async () => {
    for (let snapshotAfter = 0; snapshotAfter <= 3; snapshotAfter += 1) {
      const bridge = new FakeWelcomeBridge();
      const loading = loadWelcomeStores(bridge, () => true);
      const states = ['one', 'two', 'three'];
      for (const run of states.slice(0, snapshotAfter)) bridge.emitChrome(chrome(run));
      const snapshotRun = states[snapshotAfter - 1] ?? 'initial';
      bridge.overviewReply.resolve({
        ok: true,
        value: {
          eventCursor: snapshotAfter,
          settingsProblem: null,
          chrome: chrome(snapshotRun),
          recent: [],
          listings: [],
          host: 'local',
        },
      });
      for (const run of states.slice(snapshotAfter)) bridge.emitChrome(chrome(run));

      const loaded = await loading;
      if (!loaded.ok) throw new Error(loaded.error);
      expect(loaded.value.welcome.getState().chrome.attached?.runId).toBe('three');
    }
  });

  test('loads an authoritative listing sent before listener registration', async () => {
    const bridge = new FakeWelcomeBridge();
    bridge.emitListing(listing('initial'));
    const loading = loadWelcomeStores(bridge, () => true);
    bridge.overviewReply.resolve({
      ok: true,
      value: {
        eventCursor: bridge.cursor(),
        settingsProblem: null,
        chrome: chrome('snapshot'),
        recent: [],
        listings: [listing('initial')],
        host: 'local',
      },
    });

    const loaded = await loading;
    if (!loaded.ok) throw new Error(loaded.error);
    expect(loaded.value.lists.getState().listings).toMatchObject({
      'ssh:initial': {host: 'ssh:initial', live: [{runId: 'initial'}]},
    });
  });

  test('does not repeat a host effect already included in the final snapshot cursor', async () => {
    const bridge = new FakeWelcomeBridge();
    const loading = loadWelcomeStores(bridge, () => true);
    const cursorAtRequestStart = bridge.cursor();
    bridge.emitShowHost('ssh:included');
    expect(bridge.cursor()).toBeGreaterThan(cursorAtRequestStart);
    bridge.overviewReply.resolve({
      ok: true,
      value: {
        eventCursor: bridge.cursor(),
        settingsProblem: null,
        chrome: chrome('snapshot'),
        recent: [],
        listings: [],
        host: 'ssh:included',
      },
    });

    const loaded = await loading;
    if (!loaded.ok) throw new Error(loaded.error);
    expect(bridge.hostCalls).toEqual(['ssh:included']);
  });

  test('stops buffering after an overview failure', async () => {
    const bridge = new FakeWelcomeBridge();
    const loading = loadWelcomeStores(bridge, () => true);
    bridge.overviewReply.resolve({ok: false, error: 'settings unavailable', authNeeded: false});

    await expect(loading).resolves.toEqual({
      ok: false,
      error: 'settings unavailable',
      authNeeded: false,
    });
    bridge.emitShowHost('ssh:ignored');
    expect(bridge.hostCalls).toEqual([]);
  });
});

async function hostReady(store: Store<HostState, HostAction>): Promise<void> {
  if (store.getState().panel.kind === 'ready') return;
  await new Promise<void>(resolve => {
    const unsubscribe = store.subscribe(() => {
      if (store.getState().panel.kind !== 'ready') return;
      unsubscribe();
      resolve();
    });
  });
}

function chrome(runId: string): ChromeState {
  return {
    mode: 'run',
    attached: {
      host: `ssh:${runId}`,
      hostLabel: runId,
      instanceId: `instance-${runId}`,
      runId,
      project: `/projects/${runId}`,
      connection: {kind: 'connected'},
    },
    stops: {},
  };
}

function recent(runId: string): WelcomeRecent {
  return {
    host: `ssh:${runId}`,
    hostLabel: runId,
    project: `/projects/${runId}`,
    task: 'queue',
    runId,
    attachedAt: 1,
  };
}

function listing(runId: string): HostListing {
  return {
    kind: 'listed',
    host: `ssh:${runId}`,
    live: [
      {
        id: `instance-${runId}`,
        projectRoot: `/projects/${runId}`,
        runId,
        status: 'running',
        startedAt: 1,
        reason: null,
      },
    ],
    ended: [],
    unreadable: [],
  };
}
