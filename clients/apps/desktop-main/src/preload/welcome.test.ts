/**
 * The welcome contract, end to end: what the renderer's bridge (this preload) sends on each channel
 * is what the main process's parser (`@vibesys/ipc`'s `parseWelcomeRequest`) reads. Each bridge
 * call below goes through the preload's real factory into the real parser, for generated valid
 * arguments, and must come out as the request those arguments name; every channel and event the
 * contract declares is exercised.
 */
import {describe, expect, test} from 'bun:test';
import {createRequire} from 'node:module';
import {type HostKey, hostFromKey} from '@vibesys/hosts';
import {
  parseWelcomeRequest,
  WELCOME_CHANNELS,
  WELCOME_EVENTS,
  type WelcomeBridge,
  type WelcomeRequest,
  type WelcomeResult,
} from '@vibesys/ipc';
import {exactResumeRunId} from '@vibesys/runs';
import {SEEDS, Seeded} from '@vibesys/store/testing';

const createWelcomePreload = createRequire(import.meta.url)(
  './welcome.cjs',
) as typeof import('./welcome.cjs');

/** The preload over the main process's parser: each invoke answers with the parsed request. */
class ParsingMain {
  readonly invoked = new Set<string>();
  readonly subscribed = new Map<string, (value: unknown) => void>();

  readonly bridge: WelcomeBridge = createWelcomePreload({
    platform: 'test',
    invoke: async (channel, ...args) => {
      this.invoked.add(channel);
      try {
        return {ok: true, value: parseWelcomeRequest(channel, args)};
      } catch (error) {
        return {ok: false, error: (error as Error).message, authNeeded: false};
      }
    },
    on: (channel, listener) => {
      this.subscribed.set(channel, listener);
    },
  });
}

async function parsed(reply: Promise<WelcomeResult<unknown>>): Promise<WelcomeRequest> {
  const result = await reply;
  if (!result.ok) throw new Error(`the main process refused the bridge's call: ${result.error}`);
  return result.value as WelcomeRequest;
}

const HOSTS: readonly HostKey[] = ['local', 'ssh:gpu-box', 'ssh:user@cluster.example'];
const PATHS = ['/srv/project', '~/work/vibesys', '/', '~'];
const NAMES = ['spsc', 'a', 'run-1.b_c', '0abc'];
const INSTANCES = ['0123456789ab', 'ffffffffffff'];

/** One bridge call with valid arguments, and the request the main process must read from it. */
type Case = readonly [
  call: (bridge: WelcomeBridge) => Promise<WelcomeResult<unknown>>,
  expected: WelcomeRequest,
];

function cases(random: Seeded): Case[] {
  const key = random.pick(HOSTS);
  const host = hostFromKey(key);
  const path = random.pick(PATHS);
  const name = random.pick(NAMES);
  const instance = random.pick(INSTANCES);
  const token = 1 + random.int(1_000_000);
  const words = random.pick(['', '--rounds 2', "--note 'two words'"]);
  const force = random.pick([true, false]);
  const harness = random.pick([null, {provider: 'claude', model: 'claude-a'}]);
  const directory = path.endsWith('/') ? path : `${path}/`;
  const appearance = {
    mode: random.pick(['system', 'light', 'dark'] as const),
    light: random.pick(['paper', 'mist', 'sand'] as const),
    dark: random.pick(['ink', 'midnight', 'terminal'] as const),
  };
  return [
    [bridge => bridge.overview(), {kind: 'overview'}],
    [bridge => bridge.hosts(), {kind: 'hosts'}],
    [bridge => bridge.host(key), {kind: 'host', host}],
    [
      bridge => bridge.setCheckout(key, path, token),
      {kind: 'set-checkout', host, path, change: token},
    ],
    [bridge => bridge.cancelCheckout(token), {kind: 'cancel-checkout', change: token}],
    [bridge => bridge.cancelInspection(token), {kind: 'cancel-inspection', inspection: token}],
    [bridge => bridge.signIn(key), {kind: 'sign-in', host}],
    [bridge => bridge.complete(key, directory), {kind: 'complete', host, directory}],
    [
      bridge => bridge.inspect(key, instance, token),
      {kind: 'inspect', host, instance, inspection: token},
    ],
    [
      bridge => bridge.logs(key, instance, {kind: 'list'}),
      {kind: 'logs', host, instance, request: {kind: 'list'}},
    ],
    [bridge => bridge.releaseLogs(), {kind: 'release-logs'}],
    [bridge => bridge.tasks(key, path), {kind: 'tasks', host, project: path}],
    [bridge => bridge.harnesses(key), {kind: 'harnesses', host}],
    [
      bridge => bridge.start(key, path, name, words, harness, token),
      {kind: 'start', host, project: path, task: name, args: words, harness, start: token},
    ],
    [bridge => bridge.cancelStart(token), {kind: 'cancel-start', start: token}],
    [bridge => bridge.attach(key, instance), {kind: 'attach', host, instance}],
    [
      bridge => bridge.resume(key, path, exactResumeRunId(name)),
      {kind: 'resume', host, project: path, run: exactResumeRunId(name)},
    ],
    [bridge => bridge.showRun(), {kind: 'show-run'}],
    [bridge => bridge.showWelcome(), {kind: 'show-welcome'}],
    [bridge => bridge.retry(), {kind: 'retry'}],
    [
      bridge => bridge.stop(key, instance, force),
      {kind: 'stop', host, instance, force, confirmed: false},
    ],
    [
      bridge => bridge.stopConfirmed(key, instance, force),
      {kind: 'stop', host, instance, force, confirmed: true},
    ],
    [bridge => bridge.preferences(), {kind: 'preferences'}],
    [bridge => bridge.setAppearance(appearance), {kind: 'set-appearance', appearance}],
    [bridge => bridge.setNotifications(force), {kind: 'set-notifications', enabled: force}],
    [
      bridge => bridge.archive({host: key, runId: name}, force),
      {kind: 'archive', run: {host: key, runId: name}, archived: force},
    ],
  ];
}

describe('the welcome contract', () => {
  test('property: every bridge call is parsed by the main process as the request it names', async () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const main = new ParsingMain();
      for (const [call, expected] of cases(random)) {
        expect(await parsed(call(main.bridge))).toEqual(expected);
      }
      expect([...main.invoked].sort()).toEqual(Object.values(WELCOME_CHANNELS).sort());
    }
  });

  test('every event the main process sends reaches the listener the view registered', () => {
    const main = new ParsingMain();
    const heard: Array<readonly [string, unknown]> = [];
    const bridge = main.bridge;
    bridge.onChrome(event => heard.push([WELCOME_EVENTS.chrome, event]));
    bridge.onRecent(event => heard.push([WELCOME_EVENTS.recent, event]));
    bridge.onShowHost(event => heard.push([WELCOME_EVENTS.showHost, event]));
    bridge.onListing(event => heard.push([WELCOME_EVENTS.listing, event]));
    bridge.onPreferences(event => heard.push([WELCOME_EVENTS.preferences, event]));
    expect([...main.subscribed.keys()].sort()).toEqual(Object.values(WELCOME_EVENTS).sort());
    for (const [index, channel] of Object.values(WELCOME_EVENTS).entries()) {
      main.subscribed.get(channel)?.({sequence: index, value: channel});
    }
    expect(heard).toEqual(
      Object.values(WELCOME_EVENTS).map(
        (channel, index) => [channel, {sequence: index, value: channel}] as const,
      ),
    );
  });
});
