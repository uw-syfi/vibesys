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
  readonly connected: string[] = [];

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
    connectRun: id => {
      this.connected.push(id);
    },
    on: (channel, listener) => {
      this.subscribed.set(channel, listener);
      return () => {
        this.subscribed.delete(channel);
      };
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
  const force = random.pick([true, false]);
  const directory = path.endsWith('/') ? path : `${path}/`;
  const inputs = {
    project: path,
    task: name,
    harness: {provider: 'claude', model: 'claude-a'},
    // As the main process reads them: every option present, null when the run's own applies.
    options: random.pick([
      {rounds: null, time_limit_s: null, results: null, roles: []},
      {rounds: 2, time_limit_s: 3600, results: 'keep' as const, roles: []},
      {
        rounds: null,
        time_limit_s: null,
        results: 'publish_github' as const,
        roles: [{role: 'judge', model: null, reasoning_effort: 'high' as const}],
      },
    ]),
    agents: random.pick([{}, {judge: {provider: 'claude', model: 'claude-b'}}]),
    measure_on: random.pick([
      {kind: 'same_machine' as const},
      {kind: 'slurm' as const, cluster: 'lab-h100'},
      {kind: 'modal' as const},
    ]),
    args: random.pick([[], ['--max-in-flight', '3']]),
  };
  const cluster = {
    name,
    reach: random.pick([{kind: 'login_node' as const}, {kind: 'ssh' as const, host: 'lab-login'}]),
    workspace: path,
    sbatch_options: random.pick([[], ['--account=lab']]),
  };
  const appearance = {
    mode: random.pick(['system', 'light', 'dark'] as const),
    light: random.pick(['paper', 'mist', 'sand'] as const),
    dark: random.pick(['ink', 'midnight', 'terminal'] as const),
  };
  return [
    [bridge => bridge.overview(), {kind: 'overview'}],
    [
      bridge => bridge.setCheckout(key, path, token),
      {kind: 'set-checkout', host, path, change: token},
    ],
    [bridge => bridge.cancelCheckout(token), {kind: 'cancel-checkout', change: token}],
    [bridge => bridge.signIn(key), {kind: 'sign-in', host}],
    [bridge => bridge.retryListing(key), {kind: 'retry-listing', host}],
    [bridge => bridge.complete(key, directory), {kind: 'complete', host, directory}],
    [
      bridge => bridge.logs(key, instance, {kind: 'list'}),
      {kind: 'logs', host, instance, request: {kind: 'list'}},
    ],
    [bridge => bridge.releaseLogs(), {kind: 'release-logs'}],
    [bridge => bridge.tasks(key, path), {kind: 'tasks', host, project: path}],
    [bridge => bridge.harnesses(key), {kind: 'harnesses', host}],
    [bridge => bridge.attach(key, instance), {kind: 'attach', host, instance}],
    [
      bridge => bridge.attachReader(key, path, exactResumeRunId(name)),
      {kind: 'attach-reader', host, project: path, runId: exactResumeRunId(name)},
    ],
    [
      bridge => bridge.resume(key, path, exactResumeRunId(name)),
      {kind: 'resume', host, project: path, run: exactResumeRunId(name)},
    ],
    [bridge => bridge.releaseRun(), {kind: 'release-run'}],
    [
      bridge => bridge.readEvents(key, path, name, token, 1 + (token % 1000)),
      {kind: 'read-events', host, project: path, runId: name, from: token, max: 1 + (token % 1000)},
    ],
    [
      bridge => bridge.readTrace(key, path, name, token, 1 + (token % 1000)),
      {kind: 'read-trace', host, project: path, runId: name, from: token, max: 1 + (token % 1000)},
    ],
    [
      bridge => bridge.readSummary(key, path, name),
      {kind: 'read-summary', host, project: path, runId: name},
    ],
    [
      bridge => bridge.runDigests(key, path, [name]),
      {kind: 'run-digests', host, project: path, runIds: [name]},
    ],
    [
      bridge => bridge.projectRuns(key, path, token),
      {kind: 'project-runs', host, project: path, token},
    ],
    [bridge => bridge.cancelProjectRuns(token), {kind: 'cancel-project-runs', token}],
    [
      bridge => bridge.usageWindow(key, path, [name], 1_000, 2_000),
      {kind: 'usage-window', host, project: path, runIds: [name], since: 1_000, until: 2_000},
    ],
    [
      bridge => bridge.usageSummary(key, [path], [0, 900_000], 1_000),
      {kind: 'usage-summary', host, projects: [path], edges: [0, 900_000], now: 1_000},
    ],
    [
      bridge => bridge.usageRatesRefresh(key, force, 1_000),
      {kind: 'usage-rates-refresh', host, force, now: 1_000},
    ],
    [
      bridge => bridge.usagePricesSave(key, 2, [], [{from: name, to: 'm'}]),
      {
        kind: 'usage-prices-save',
        host,
        baseRevision: 2,
        overrides: [],
        aliases: [{from: name, to: 'm'}],
      },
    ],
    [
      bridge => bridge.readSpan(key, path, name, 'op:turn', 1, 100),
      {kind: 'read-span', host, project: path, runId: name, spanId: 'op:turn', from: 1, max: 100},
    ],
    [bridge => bridge.preferences(), {kind: 'preferences'}],
    [bridge => bridge.setAppearance(appearance), {kind: 'set-appearance', appearance}],
    [bridge => bridge.setNotifications(force), {kind: 'set-notifications', enabled: force}],
    [
      bridge => bridge.archive({host: key, runId: name}, force),
      {kind: 'archive', run: {host: key, runId: name}, archived: force},
    ],
    [bridge => bridge.machines(), {kind: 'machines'}],
    [bridge => bridge.machine(key), {kind: 'machine', host}],
    [bridge => bridge.inspectCheckout(key, path), {kind: 'inspect-checkout', host, path}],
    [bridge => bridge.removeMachine(key), {kind: 'remove-machine', host}],
    [bridge => bridge.compute(key), {kind: 'compute', host}],
    [bridge => bridge.testCluster(key, cluster), {kind: 'test-cluster', host, cluster}],
    [bridge => bridge.addCluster(key, cluster), {kind: 'add-cluster', host, cluster}],
    [bridge => bridge.removeCluster(key, name), {kind: 'remove-cluster', host, name}],
    [bridge => bridge.agentLogins(key), {kind: 'agent-logins', host}],
    [bridge => bridge.startLogin(key, name), {kind: 'start-login', host, provider: name}],
    [bridge => bridge.describeMachine(key), {kind: 'describe-machine', host}],
    [bridge => bridge.preflight(key, inputs), {kind: 'preflight', host, inputs}],
    [bridge => bridge.examples(key), {kind: 'examples', host}],
    [
      bridge => bridge.createFromExample(key, name, path),
      {kind: 'create-from-example', host, example: name, destination: path},
    ],
    [
      bridge => bridge.copyOut(key, path, `${directory}copy`),
      {kind: 'copy-out', host, source: path, destination: `${directory}copy`},
    ],
    [bridge => bridge.startRun(key, inputs), {kind: 'start-run', host, inputs}],
    [bridge => bridge.helperFeatures(key), {kind: 'helper-features', host}],
    [
      bridge => bridge.taskGoal(key, path, name),
      {kind: 'task-goal', host, project: path, task: name},
    ],
    [
      bridge => bridge.exampleDestination(key, name, path),
      {kind: 'example-destination', host, example: name, destination: path},
    ],
    [
      bridge => bridge.openRunWindow({host: key, runId: name}),
      {kind: 'open-run-window', run: {host: key, runId: name}},
    ],
    [bridge => bridge.showMainWindow(), {kind: 'show-main-window'}],
    [bridge => bridge.openSettings(), {kind: 'open-settings'}],
    [
      bridge => bridge.openExternal('https://example.com/a?b=1'),
      {kind: 'open-external', url: 'https://example.com/a?b=1'},
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

  test('a run connection asks the preload for a channel, and a wake reaches the view', () => {
    const main = new ParsingMain();
    let woke = 0;
    main.bridge.connectRun('run-1');
    main.bridge.onWake(() => {
      woke += 1;
    });
    main.subscribed.get('vibesys:wake')?.(undefined);
    expect(main.connected).toEqual(['run-1']);
    expect(woke).toBe(1);
  });

  test('a run window can request Settings in the main window', () => {
    const main = new ParsingMain();
    let opened = 0;
    main.bridge.onOpenSettings(() => {
      opened += 1;
    });
    main.subscribed.get('vibesys:open-settings')?.(undefined);
    expect(opened).toBe(1);
  });

  test('every event the main process sends reaches the listener the view registered', () => {
    const main = new ParsingMain();
    const heard: Array<readonly [string, unknown]> = [];
    const bridge = main.bridge;
    bridge.onRecent(event => heard.push([WELCOME_EVENTS.recent, event]));
    bridge.onListing(event => heard.push([WELCOME_EVENTS.listing, event]));
    bridge.onPreferences(event => heard.push([WELCOME_EVENTS.preferences, event]));
    bridge.onHostSettingsChanged(() => heard.push([WELCOME_EVENTS.hostSettings, null]));
    expect([...main.subscribed.keys()].sort()).toEqual(Object.values(WELCOME_EVENTS).sort());
    for (const [index, channel] of Object.values(WELCOME_EVENTS).entries()) {
      main.subscribed.get(channel)?.({sequence: index, value: channel});
    }
    expect(heard).toEqual(
      Object.values(WELCOME_EVENTS).map(
        (channel, index) =>
          [
            channel,
            channel === WELCOME_EVENTS.hostSettings ? null : {sequence: index, value: channel},
          ] as const,
      ),
    );
  });
});
