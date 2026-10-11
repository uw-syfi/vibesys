import {describe, expect, test} from 'bun:test';
import {exactResumeRunId, type HostKey, type HostListing, type RecentRun} from '@vibesys/entities';
import type {ChromeState, WelcomeBridge, WelcomeResult} from '@vibesys/ipc';
import {welcomeStores} from './stores.js';

const CHROME: ChromeState = {mode: 'welcome', attached: null, stops: {}};

function ok<T>(value: T): Promise<WelcomeResult<T>> {
  return Promise.resolve({ok: true, value});
}

/**
 * A welcome bridge whose requests answer at once and are recorded by name, and whose events the
 * test pushes as the main process would.
 */
class FakeWelcomeBridge implements WelcomeBridge {
  readonly platform = 'darwin';
  readonly asked: string[] = [];
  #chrome: ((state: ChromeState) => void) | null = null;
  #recent: ((recent: readonly RecentRun[]) => void) | null = null;
  #showHost: ((host: HostKey) => void) | null = null;
  #listing: ((listing: HostListing) => void) | null = null;

  overview = () => ok({settingsProblem: null, recent: [], chrome: CHROME, host: 'local'});
  hosts = () => this.#ask('hosts', ['gpu-box']);
  host = (host: HostKey) => this.#ask(`host ${host}`, {key: host, label: host, checkout: '/c'});
  setCheckout = (host: HostKey, path: string) => this.#ask(`set-checkout ${host}`, path);
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
  onChrome = (listener: (state: ChromeState) => void) => {
    this.#chrome = listener;
  };
  onRecent = (listener: (recent: readonly RecentRun[]) => void) => {
    this.#recent = listener;
  };
  onShowHost = (listener: (host: HostKey) => void) => {
    this.#showHost = listener;
  };
  onListing = (listener: (listing: HostListing) => void) => {
    this.#listing = listener;
  };

  pushChrome(state: ChromeState): void {
    this.#chrome?.(state);
  }
  pushRecent(recent: readonly RecentRun[]): void {
    this.#recent?.(recent);
  }
  pushShowHost(host: HostKey): void {
    this.#showHost?.(host);
  }
  pushListing(listing: HostListing): void {
    this.#listing?.(listing);
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

const RUN: RecentRun = {
  host: 'ssh:gpu',
  hostLabel: 'gpu',
  project: '/p',
  task: 'spsc',
  runId: 'run-1',
  attachedAt: 1,
};

describe('welcome stores', () => {
  test('the first host is shown and asked for at once', async () => {
    const bridge = new FakeWelcomeBridge();
    const stores = welcomeStores(bridge, () => true, 'ssh:gpu');
    await settle();
    expect(bridge.asked).toEqual(['host ssh:gpu']);
    expect(stores.host.getState().panel.kind).toBe('ready');
  });

  test('the main process pushes reach the stores that fold them', async () => {
    const bridge = new FakeWelcomeBridge();
    const stores = welcomeStores(bridge, () => true, 'local');
    bridge.pushRecent([RUN]);
    bridge.pushListing({kind: 'listed', host: 'ssh:gpu', live: [], ended: [], unreadable: []});
    bridge.pushShowHost('ssh:gpu');
    await settle();
    expect(stores.lists.getState().runs).toEqual([RUN]);
    expect(stores.lists.getState().listings['ssh:gpu']?.kind).toBe('listed');
    expect(stores.host.getState().host).toBe('ssh:gpu');
    expect(bridge.asked).toContain('host ssh:gpu');
  });

  test('a run filling the window closes the picker and the start sheet', async () => {
    const bridge = new FakeWelcomeBridge();
    const stores = welcomeStores(bridge, () => true, 'local');
    stores.picker.dispatch({type: 'open'});
    stores.start.dispatch({type: 'open', host: {key: 'local', label: 'This Mac'}, projects: []});
    await settle();
    expect(stores.picker.getState().kind).toBe('open');
    expect(stores.start.getState().kind).toBe('editing');
    bridge.pushChrome({...CHROME, mode: 'run'});
    expect(stores.welcome.getState().chrome.mode).toBe('run');
    expect(stores.picker.getState().kind).toBe('closed');
    expect(stores.start.getState().kind).toBe('closed');
  });

  test('choosing a host in the picker shows its panel', async () => {
    const bridge = new FakeWelcomeBridge();
    const stores = welcomeStores(bridge, () => true, 'local');
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
    const stores = welcomeStores(
      bridge,
      question => {
        questions.push(question);
        return false;
      },
      'local',
    );
    stores.welcome.dispatch({
      type: 'command',
      command: {kind: 'resume', host: 'local', project: '/p', run: exactResumeRunId('run-1')},
    });
    await settle();
    expect(questions).toHaveLength(1);
    expect(bridge.asked).not.toContain('resume');
    expect(stores.welcome.getState().awaiting).toBeNull();
  });
});
