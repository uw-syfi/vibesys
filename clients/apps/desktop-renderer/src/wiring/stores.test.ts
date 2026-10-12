import {describe, expect, test} from 'bun:test';
import type {HostAction, HostState} from '@vibesys/feature-hosts/state';
import type {HostKey, HostListing} from '@vibesys/hosts';
import type {
  ChromeState,
  WelcomeBridge,
  WelcomeEvent,
  WelcomeOverview,
  WelcomeResult,
} from '@vibesys/ipc';
import {exactResumeRunId, type RecentRun} from '@vibesys/runs';
import type {Store} from '@vibesys/store';
import {loadWelcomeStores, type WelcomeStores} from './stores.js';

const WELCOME_CHROME: ChromeState = {mode: 'welcome', attached: null, stops: {}};

function ok<T>(value: T): Promise<WelcomeResult<T>> {
  return Promise.resolve({ok: true, value});
}

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

/**
 * A welcome bridge whose requests answer at once and are recorded by name, whose overview the test
 * answers, and whose events the test pushes as the main process would: each with the next sequence.
 */
class FakeWelcomeBridge implements WelcomeBridge {
  readonly platform = 'darwin';
  readonly asked: string[] = [];
  readonly overviewReply = new Deferred<WelcomeResult<WelcomeOverview>>();
  readonly checkoutCalls: Array<{
    readonly host: HostKey;
    readonly path: string;
    readonly change: number;
    readonly reply: Deferred<WelcomeResult<string>>;
  }> = [];
  readonly cancelledCheckouts: number[] = [];
  checkout = '/c';
  readonly #chrome: Array<(event: WelcomeEvent<ChromeState>) => void> = [];
  readonly #recent: Array<(event: WelcomeEvent<readonly RecentRun[]>) => void> = [];
  readonly #showHost: Array<(event: WelcomeEvent<HostKey>) => void> = [];
  readonly #listing: Array<(event: WelcomeEvent<HostListing>) => void> = [];
  #sequence = 0;

  overview = () => this.overviewReply.promise;
  hosts = () => this.#ask('hosts', ['gpu-box']);
  host = (host: HostKey) =>
    this.#ask(`host ${host}`, {key: host, label: host, checkout: this.checkout});
  setCheckout = (host: HostKey, path: string, change: number) => {
    this.asked.push(`set-checkout ${host}`);
    const reply = new Deferred<WelcomeResult<string>>();
    this.checkoutCalls.push({host, path, change, reply});
    return reply.promise;
  };
  cancelCheckout = (change: number) => {
    this.cancelledCheckouts.push(change);
    return this.#ask('cancel-checkout', null);
  };
  signIn = (host: HostKey) => this.#ask(`sign-in ${host}`, null);
  tasks = (host: HostKey, project: string) => this.#ask(`tasks ${host} ${project}`, ['spsc']);
  complete = (host: HostKey, directory: string) =>
    this.#ask(`complete ${host} ${directory}`, {directory, entries: [], truncated: false});
  attach = (host: HostKey, instance: string) => this.#ask(`attach ${host} ${instance}`, null);
  start = () => this.#ask('start', 'started' as const);
  cancelStart = () => this.#ask('cancel-start', null);
  resume = () => this.#ask('resume', null);
  showRun = () => this.#ask('show-run', null);
  showWelcome = () => this.#ask('show-welcome', null);
  retry = () => this.#ask('retry', null);
  stop = () => this.#ask('stop', null);
  onChrome = (listener: (event: WelcomeEvent<ChromeState>) => void) => {
    this.#chrome.push(listener);
  };
  onRecent = (listener: (event: WelcomeEvent<readonly RecentRun[]>) => void) => {
    this.#recent.push(listener);
  };
  onShowHost = (listener: (event: WelcomeEvent<HostKey>) => void) => {
    this.#showHost.push(listener);
  };
  onListing = (listener: (event: WelcomeEvent<HostListing>) => void) => {
    this.#listing.push(listener);
  };

  listenerCounts(): number[] {
    return [this.#chrome.length, this.#recent.length, this.#listing.length, this.#showHost.length];
  }

  /** Answer the overview with the events sent so far covered, and `values` in its snapshot. */
  answerOverview(values: Partial<Omit<WelcomeOverview, 'eventCursor'>> = {}): void {
    this.overviewReply.resolve({
      ok: true,
      value: {
        eventCursor: this.#sequence,
        settingsProblem: null,
        chrome: WELCOME_CHROME,
        recent: [],
        listings: [],
        host: 'local',
        ...values,
      },
    });
  }

  pushChrome(value: ChromeState): void {
    const event = this.#event(value);
    for (const listener of this.#chrome) listener(event);
  }

  pushRecent(value: readonly RecentRun[]): void {
    const event = this.#event(value);
    for (const listener of this.#recent) listener(event);
  }

  pushShowHost(value: HostKey): void {
    const event = this.#event(value);
    for (const listener of this.#showHost) listener(event);
  }

  pushListing(value: HostListing): void {
    const event = this.#event(value);
    for (const listener of this.#listing) listener(event);
  }

  #event<T>(value: T): WelcomeEvent<T> {
    this.#sequence += 1;
    return {sequence: this.#sequence, value};
  }

  #ask<T>(request: string, value: T): Promise<WelcomeResult<T>> {
    this.asked.push(request);
    return ok(value);
  }
}

/** Let the bridge's resolved replies reach their stores. */
async function settle(): Promise<void> {
  for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
}

/** Load the stores over `bridge` with an overview showing `host` first. */
async function loaded(
  bridge: FakeWelcomeBridge,
  confirm: (question: string) => boolean = () => true,
  host: HostKey = 'local',
): Promise<WelcomeStores> {
  const loading = loadWelcomeStores(bridge, confirm);
  bridge.answerOverview({host});
  const result = await loading;
  if (!result.ok) throw new Error(result.error);
  return result.value;
}

describe('loadWelcomeStores', () => {
  test('cancelling an in-flight checkout revokes the same main-process operation', async () => {
    const bridge = new FakeWelcomeBridge();
    const stores = await loaded(bridge);
    await hostReady(stores.host);

    stores.host.dispatch({type: 'edit-checkout'});
    stores.host.dispatch({type: 'checkout-input', value: '/new'});
    stores.host.dispatch({type: 'submit-checkout'});
    const call = bridge.checkoutCalls[0];
    if (call === undefined) throw new Error('checkout call expected');
    expect(call).toMatchObject({host: 'local', path: '/new'});

    stores.host.dispatch({type: 'cancel-checkout'});
    expect(bridge.cancelledCheckouts).toEqual([call.change]);
    expect(stores.host.getState().panel).toMatchObject({
      kind: 'ready',
      data: {checkout: '/c'},
      checkout: null,
    });

    // Cancellation can lose the main process's atomic commit race. Its successful original reply
    // must update the closed panel to the durable checkout rather than leave renderer and disk split.
    const committed = new Promise<void>(resolve => {
      const unsubscribe = stores.host.subscribe(() => {
        const panel = stores.host.getState().panel;
        if (panel.kind !== 'ready' || panel.data.checkout !== '/new') return;
        unsubscribe();
        resolve();
      });
    });
    bridge.checkout = '/new';
    call.reply.resolve({ok: true, value: '/new'});
    await committed;
    expect(stores.host.getState().panel).toMatchObject({
      kind: 'ready',
      data: {checkout: '/new'},
      checkout: null,
    });
  });

  test('the first host is shown and asked for at once', async () => {
    const bridge = new FakeWelcomeBridge();
    const stores = await loaded(bridge, () => true, 'ssh:gpu');
    await settle();
    expect(bridge.asked).toEqual(['host ssh:gpu']);
    expect(stores.host.getState().panel.kind).toBe('ready');
  });

  test('the main process pushes reach the stores that fold them', async () => {
    const bridge = new FakeWelcomeBridge();
    const stores = await loaded(bridge);
    const run = recent('run-1');
    bridge.pushRecent([run]);
    bridge.pushListing({
      kind: 'listed',
      host: 'ssh:gpu',
      live: [],
      ended: [],
      unreadable: [],
      unverified: [],
    });
    bridge.pushShowHost('ssh:gpu');
    await settle();
    expect(stores.lists.getState().runs).toEqual([run]);
    expect(stores.lists.getState().listings['ssh:gpu']?.kind).toBe('listed');
    expect(stores.host.getState().host).toBe('ssh:gpu');
    expect(bridge.asked).toContain('host ssh:gpu');
  });

  test('a run filling the window closes the picker and the start sheet', async () => {
    const bridge = new FakeWelcomeBridge();
    const stores = await loaded(bridge);
    stores.picker.dispatch({type: 'open'});
    stores.start.dispatch({type: 'open', host: {key: 'local', label: 'This Mac'}, projects: []});
    await settle();
    expect(stores.picker.getState().kind).toBe('open');
    expect(stores.start.getState().kind).toBe('editing');
    bridge.pushChrome({...WELCOME_CHROME, mode: 'run'});
    expect(stores.welcome.getState().chrome.mode).toBe('run');
    expect(stores.picker.getState().kind).toBe('closed');
    expect(stores.start.getState().kind).toBe('closed');
  });

  test('choosing a host in the picker shows its panel', async () => {
    const bridge = new FakeWelcomeBridge();
    const stores = await loaded(bridge);
    stores.picker.dispatch({type: 'open'});
    await settle();
    stores.picker.dispatch({type: 'choose', index: 0});
    await settle();
    expect(stores.host.getState().host).toBe('ssh:gpu-box');
    expect(bridge.asked).toContain('host ssh:gpu-box');
  });

  test('a declined resume asks the main process nothing', async () => {
    const bridge = new FakeWelcomeBridge();
    const questions: string[] = [];
    const stores = await loaded(bridge, question => {
      questions.push(question);
      return false;
    });
    stores.welcome.dispatch({
      type: 'command',
      command: {kind: 'resume', host: 'local', project: '/p', run: exactResumeRunId('run-1')},
    });
    await settle();
    expect(questions).toHaveLength(1);
    expect(bridge.asked).not.toContain('resume');
    expect(stores.welcome.getState().busy).toBe(false);
  });

  test('keeps every push that crosses the overview and continues live', async () => {
    const bridge = new FakeWelcomeBridge();
    const loading = loadWelcomeStores(bridge, () => true);

    expect(bridge.listenerCounts()).toEqual([1, 1, 1, 1]);
    bridge.pushChrome(chrome('covered'));
    bridge.pushRecent([recent('covered')]);
    bridge.pushShowHost('ssh:covered');
    bridge.answerOverview({chrome: chrome('snapshot'), recent: [recent('snapshot')]});
    // These messages are newer than the snapshot, but arrive before its promise continuation can
    // build the stores. They must be buffered rather than disappearing into the bootstrap gap.
    bridge.pushChrome(chrome('newer'));
    bridge.pushRecent([recent('newer')]);
    bridge.pushListing(listing('newer'));
    bridge.pushShowHost('ssh:newer');

    const result = await loading;
    if (!result.ok) throw new Error(result.error);
    const stores = result.value;
    expect(stores.welcome.getState().chrome.attached?.runId).toBe('newer');
    expect(stores.lists.getState()).toMatchObject({
      runs: [{runId: 'newer'}],
      listings: {'ssh:newer': {host: 'ssh:newer', live: [{runId: 'newer'}]}},
    });
    expect(stores.host.getState().host).toBe('ssh:newer');

    bridge.pushChrome(chrome('live'));
    bridge.pushRecent([recent('live')]);
    expect(stores.welcome.getState().chrome.attached?.runId).toBe('live');
    expect(stores.lists.getState().runs[0]?.runId).toBe('live');
  });

  test('converges for every snapshot position in an ordered push sequence', async () => {
    const states = ['one', 'two', 'three'];
    for (let snapshotAfter = 0; snapshotAfter <= states.length; snapshotAfter += 1) {
      const bridge = new FakeWelcomeBridge();
      const loading = loadWelcomeStores(bridge, () => true);
      for (const run of states.slice(0, snapshotAfter)) bridge.pushChrome(chrome(run));
      bridge.answerOverview({chrome: chrome(states[snapshotAfter - 1] ?? 'initial')});
      for (const run of states.slice(snapshotAfter)) bridge.pushChrome(chrome(run));

      const result = await loading;
      if (!result.ok) throw new Error(result.error);
      expect(result.value.welcome.getState().chrome.attached?.runId).toBe('three');
    }
  });

  test('loads an authoritative listing sent before listener registration', async () => {
    const bridge = new FakeWelcomeBridge();
    bridge.pushListing(listing('initial'));
    const loading = loadWelcomeStores(bridge, () => true);
    bridge.answerOverview({chrome: chrome('snapshot'), listings: [listing('initial')]});

    const result = await loading;
    if (!result.ok) throw new Error(result.error);
    expect(result.value.lists.getState().listings).toMatchObject({
      'ssh:initial': {host: 'ssh:initial', live: [{runId: 'initial'}]},
    });
  });

  test('does not repeat a host effect already included in the snapshot cursor', async () => {
    const bridge = new FakeWelcomeBridge();
    const loading = loadWelcomeStores(bridge, () => true);
    bridge.pushShowHost('ssh:included');
    bridge.answerOverview({chrome: chrome('snapshot'), host: 'ssh:included'});

    const result = await loading;
    if (!result.ok) throw new Error(result.error);
    await settle();
    expect(bridge.asked).toEqual(['host ssh:included']);
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
    bridge.pushShowHost('ssh:ignored');
    await settle();
    expect(bridge.asked).toEqual([]);
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

function recent(runId: string): RecentRun {
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
    unverified: [],
  };
}
