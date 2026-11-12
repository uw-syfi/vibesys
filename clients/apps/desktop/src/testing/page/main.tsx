/**
 * The window over a fake daemon, as a browser page: for looking at the screens and driving them
 * with Playwright without Electron. Built by `vite build --mode fixture` into `dist/fixture`.
 * `?scenario=<seed>-<subagents>-<live|done>[-tree[-<depth>]]` picks the run (`tree` has agents spawn
 * each other and uses every kind of tool call; `depth` makes histories longer);
 * `cost=<priced|ended|none|empty|long|old|absent>` picks the cost the host serves for it (priced and
 * unpriced turns when absent from the address; `old` is a host without the cost stream, `absent`
 * one that has no view for the run); one `host=<key>` per host to show (this machine when none);
 * `fleet=<runs>-<live>` gives each host that many runs instead (`long` names every third run very
 * long, `down=<n>` has the host at that position lose its link after its first snapshot,
 * `leftOut=<n>` has each host say its snapshot left that many older runs out);
 * `generic` adds one generic-only run to the host's independent typed run index;
 * `turnWindow=<n>` limits run history to its earliest n turns while agent streams still serve
 * their items, as a run beyond the host's bounded timeline does;
 * Every host serves the cost its own runs add up to (`served-host-cost.ts`), so the Cost page and the Runs
 * page agree; `fleet=0-0` is a host with no runs.
 * `report` has the lead post a report about the whole run, which the report screen sums the run up by
 * (a Catch me up answer, a window report, never replaces it);
 * `catchUp=<none|history|writing|written|pending|failed>` gives the viewer check-ins already made
 * on the run (none when absent), and a press of Catch me up makes another that stays pending while
 * the lead writes; `measured=<candidates>` gives the run that many measured candidates on two
 * objectives, with the lead's bounds, so the report has its charts and tiles (the head is the best
 * of the first); `stale[=<reason>]` has the host say it cannot read the run's job log, for the
 * `UnreadableReason` of that name (`STREAM_LOST` when it names none, and always without
 * `measured`), and `staleDetail=<words>` gives its own words about it (with `measured`); a name
 * that is no reason fails the page; `appearance=<mode>-<light theme>-<dark theme>` sets what is
 * saved. Saved appearance lives in memory, so a reload starts again from the address.
 * `machines=<one|several|unsupported>` is a world for the Machines page (`machine-cases.ts`): `one` is this
 * machine live with its readings and four places; `several` is five machines (live, reconnecting, refused,
 * a very long alias) with places reachable and not; `unsupported` is a machine whose host cannot sample
 * its stats. It names the hosts shown, so `host` parameters are ignored; open Machines to see it.
 * `case=<started|failed|week>` is one of three runs the report must hold in one layout (a run that has
 * just started with nothing measured; one that failed within a minute and a half with three
 * measurements; one of six days with two objectives, 400 measurements served of 3,000, 64 ideas and 28
 * agents): it replaces the scenario, the search view and the cost, and no other parameter changes them.
 * `spend=<none|short|week>` is one of three hosts the Cost page must hold in one layout (one run that
 * has finished no turn; one short run of nine turns, four unpriced; sixteen runs over a week on
 * four models and two tools, some agents unpriced): it replaces the scenario.
 */
import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
import {ReportKind, UnreadableReason, UnreadableReasonSchema} from '@vibesys/orchestrator-proto';
import {DesktopLinkProblem, Direction, RunLiveness, SeriesRole} from '@vibesys/proto';
import {type Appearance, parseAppearance} from '@vibesys/ui/tokens';
import * as Clock from 'effect/Clock';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import * as Scope from 'effect/Scope';
import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {RpcStatus} from '../../core/errors.js';
import {
  Ids,
  makeAppearance,
  makeHostSession,
  makeMachineFolders,
  makeNavigation,
  preferencesOver,
  Viewer,
  viewerOver,
} from '../../core/index.js';
import type {CostView} from '../../state/index.js';
import {hostsOfAddress} from '../../ui/address.js';
import {DisplayClockProvider} from '../../ui/display-clock.js';
import type {ConnectedHost} from '../../ui/orchestrator/connected.js';
import {
  KeyPressesFromWindow,
  SystemAppearanceFromWindow,
  ThemeDocumentLive,
} from '../../ui/shell/browser-layers.js';
import {ConnectedShell} from '../../ui/shell/connected.js';
import {MACHINE_CASES, machineCaseHosts, machineCaseRecording} from '../machine-cases.js';
import {CATCH_UP_SHAPES, seedCheckIns} from '../orchestrator/check-in-fixtures.js';
import {
  ENDED_RUN,
  LONG_NAMES,
  NO_PRICES,
  NO_TURNS,
  PRICED_AND_UNPRICED,
} from '../orchestrator/cost-views.js';
import {
  FakeSetup,
  SETUP_FLAVORS,
  type SetupFlavor,
  setupScenario,
} from '../orchestrator/fake-setup.js';
import {
  COST_CASES,
  costCaseScenario,
  FAKE_VIEWER,
  FakeDaemon,
  FIXTURE_FILES,
  fleetScenario,
  generateScenario,
  measuredSearch,
  methodPathOf,
  REPORT_CASES,
  reportCaseWorld,
  SERVING,
  START_MS,
  withReport,
} from '../orchestrator/index.js';
import {daemonRecording} from '../orchestrator/recordings.js';
import {servedCost} from '../orchestrator/served-cost.js';
import {servedHostCost} from '../orchestrator/served-host-cost.js';
import {runEntryFrame, runsSyncedFrame, servingRuns} from '../runs-topic.js';
import {makeFakePreferencesBridge} from '../settings/fakes.js';
import {fixtureMachineActions} from '../settings/machine-actions.js';
import {memoryFolders} from '../settings/machine-folders.js';
import {makeFakeTransport, type Recording} from '../transport/index.js';
import {makeFakeViewerBridge} from '../viewer/fakes.js';

const DEFAULT_SCENARIO = '5-4-live';
const THIS_MACHINE = {key: 'local', label: 'This machine'} as const;

function scenarioOf(search: string) {
  const [seed = '5', subagents = '4', mode = 'live', shape = 'flat', depth = '0'] = (
    new URLSearchParams(search).get('scenario') ?? DEFAULT_SCENARIO
  ).split('-');
  return generateScenario({
    seed: Number(seed),
    subagents: Number(subagents),
    live: mode === 'live',
    tree: shape === 'tree',
    depth: Number(depth),
  });
}

/** The appearance the address says is saved, if it says one. */
function savedAppearance(query: URLSearchParams): Appearance | undefined {
  const given = query.get('appearance');
  if (given === null) return undefined;
  const [mode, light, dark] = given.split('-');
  return parseAppearance({mode, light, dark}, 'appearance');
}

const COSTS: Readonly<Record<string, CostView>> = {
  priced: PRICED_AND_UNPRICED,
  ended: ENDED_RUN,
  none: NO_PRICES,
  empty: NO_TURNS,
  long: LONG_NAMES,
};

const query = new URLSearchParams(window.location.search);

/**
 * `setup=<ticks|refused|unreadable|pending>` opens a run in its setup phase with the host's scripted
 * setup world (`fake-setup.ts`): each message sent to the lead moves its six checks on one step. Every
 * page has that world for New run's Set up a task, which begins one.
 */
const setupFlavor: SetupFlavor | undefined = SETUP_FLAVORS.find(
  each => each === query.get('setup'),
);

/** The report case the address names, with its scripted world; none when it names none. */
const reportCase = REPORT_CASES.find(each => each === query.get('case'));
const caseWorld = reportCase === undefined ? undefined : reportCaseWorld(reportCase);

/** The host the address names for the Cost page (`spend=none|short|week`); none when it names none. */
const costCase = COST_CASES.find(each => each === query.get('spend'));

function baseScenario() {
  if (caseWorld !== undefined) return caseWorld.scenario;
  if (costCase !== undefined) return costCaseScenario(costCase);
  return setupFlavor === undefined ? scenarioOf(window.location.search) : setupScenario();
}

/**
 * The page's wall clock: the moment the scenario ends plus a minute, frozen, so a scenario reads the
 * same every time it is opened (ages, times of day, who is fresh). The fleet's runs are placed
 * against it and the screens read it as Effect's `Clock`.
 */
const NOW_MS = baseScenario().endMs + 60_000;
// The moment the page means by "now". The screens read their ages from the browser's clock, which would
// say the scenario (a fixed date) is days or years old, so the page sets that clock to the scenario's
// "now" when it loads and lets it run on from there. This is the fixture page only: no product code
// reads a clock differently, and a test never reaches it.
const clockOrigin = Date.now();
const realNow = Date.now.bind(Date);
Date.now = () => NOW_MS + (realNow() - clockOrigin);
document.documentElement.dataset.nowMs = String(NOW_MS);
const frozenClock: Clock.Clock = Object.assign(Object.create(Clock.Clock.defaultValue()), {
  currentTimeMillisUnsafe: () => NOW_MS,
  currentTimeMillis: Effect.succeed(NOW_MS),
  currentTimeNanosUnsafe: () => BigInt(NOW_MS) * 1_000_000n,
  currentTimeNanos: Effect.succeed(BigInt(NOW_MS) * 1_000_000n),
});
/** The Machines world the address names, if it names one. */
const machineCase = MACHINE_CASES.find(each => each === query.get('machines'));
const named = hostsOfAddress(window.location.search);
const shown =
  machineCase !== undefined
    ? machineCaseHosts(machineCase)
    : named.length === 0
      ? [THIS_MACHINE]
      : named;

/** The host at position `down` loses its link after its first snapshot: its run stream ends. */
const downAt = query.has('down') ? Number(query.get('down')) : undefined;

/** Each host's own fleet, from `fleet=<runs>-<live>`: the same shape, a different seed. */
function fleetRecording(spec: string): Recording {
  const [runs = '12', live = '4'] = spec.split('-');
  const nowMs = NOW_MS;
  const leftOut = Number(query.get('leftOut') ?? 0);
  const bases = new Map(
    shown.map((host, index) => {
      const hostScenario = fleetScenario({
        seed: 11 + index,
        prefix: `h${index}`,
        runs: Number(runs),
        live: Number(live),
        nowMs,
        ...(query.has('long') ? {longEvery: 3} : {}),
      });
      const daemon = new FakeDaemon(hostScenario);
      daemon.publishHostCost(servedHostCost(hostScenario.changes, nowMs));
      daemon.runsLeftOut = leftOut;
      return [host.key, daemonRecording(daemon, {files: FIXTURE_FILES, live: true})] as const;
    }),
  );
  // The transport counts calls to a method across hosts; a host's own count says which call is its first.
  const calls = new Map<string, number>();
  return {
    respond: (target, nth) => {
      const steps = bases.get(target.host)?.respond(target, nth) ?? [];
      const down = downAt !== undefined && shown[downAt]?.key === target.host;
      const own = calls.get(`${target.host}|${target.method}`) ?? 0;
      calls.set(`${target.host}|${target.method}`, own + 1);
      if (!down) return steps;
      if (target.method === methodPathOf('Hello') && own > 0)
        return [{end: {message: 'link lost', status: RpcStatus.UNAVAILABLE}}];
      return target.method === methodPathOf('WatchRuns')
        ? [...steps.slice(0, -1), {end: {message: 'link lost', status: RpcStatus.UNAVAILABLE}}]
        : steps;
    },
  };
}

const base = baseScenario();
const scenario =
  caseWorld === undefined && query.has('report')
    ? withReport(base, {
        sequence: 1,
        kind: ReportKind.PROGRESS,
        atMs: base.endMs - 20 * 60_000,
        headline: 'Throughput is 2.3 times the baseline at p99 118 ms.',
        changed: 'Fused expert dispatch landed.',
      })
    : base;
const measured = query.get('measured');
/** The reason `stale=<name>` names: the link lost when it names none; a wrong name fails the page. */
function staleReason(name: string): UnreadableReason {
  if (name === '') return UnreadableReason.STREAM_LOST;
  const named = UnreadableReasonSchema.values.find(value => value.localName === name);
  if (named === undefined) throw new Error(`stale=${name} names no UnreadableReason`);
  return named.number;
}
const unreadable = {
  stale: query.has('stale'),
  reason: staleReason(query.get('stale') ?? ''),
  detail: query.get('staleDetail') ?? '',
};
const daemon = new FakeDaemon({
  ...scenario,
  search: true,
  searchStale: query.has('stale'),
  searchTruncated: query.has('truncated'),
  ...(caseWorld === undefined ? {} : {searches: [caseWorld.search]}),
  ...(measured === null || caseWorld !== undefined
    ? {}
    : {
        searches: [
          measuredSearch(
            scenario.runId,
            {
              seed: 4,
              candidates: Number(measured),
              startMs: START_MS,
              endMs: scenario.endMs,
              objectives: SERVING,
              ...(query.has('ideas') ? {ideas: Number(query.get('ideas'))} : {}),
            },
            unreadable,
          ),
        ],
      }),
});
const turnWindow = query.get('turnWindow');
if (turnWindow !== null) {
  const limit = Number(turnWindow);
  if (!Number.isSafeInteger(limit) || limit < 0)
    throw new Error('turnWindow must be a non-negative integer');
  daemon.historyTurnLimit = limit;
}
const world = new FakeSetup(daemon.ledger(), setupFlavor ?? 'ticks');
daemon.setup = world;
if (setupFlavor !== undefined) world.adopt(scenario.runId);
const cost =
  caseWorld === undefined
    ? COSTS[query.get('cost') ?? (setupFlavor === undefined ? 'priced' : 'empty')]
    : caseWorld.cost;
if (cost !== undefined) daemon.publishCost(servedCost(cost, scenario.runId));
daemon.publishHostCost(servedHostCost(scenario.changes, NOW_MS));
const shape = query.get('catchUp') ?? 'none';
seedCheckIns(daemon, scenario, CATCH_UP_SHAPES.find(each => each === shape) ?? 'none');
const served = daemonRecording(daemon, {live: true, files: FIXTURE_FILES});
/** A host that predates the cost stream answers every other method and not this one. */
const withoutCostStream = {
  respond: (...call: Parameters<typeof served.respond>) =>
    call[0].method === methodPathOf('WatchRunCost')
      ? [{end: {message: 'no such method', status: RpcStatus.UNIMPLEMENTED}}]
      : served.respond(...call),
};
const fleetSpec = query.get('fleet');
const selectedRecording =
  fleetSpec !== null
    ? fleetRecording(fleetSpec)
    : query.has('login-failed')
      ? {
          respond: () => [
            {
              end: {
                linkProblem: DesktopLinkProblem.AUTH,
                message: 'Permission denied (publickey).',
              },
            },
          ],
        }
      : query.get('cost') === 'old'
        ? withoutCostStream
        : machineCase === undefined
          ? served
          : machineCaseRecording(machineCase, served);
const recording = query.has('generic')
  ? servingRuns(() => [
      runEntryFrame('generic-run', {
        liveness: RunLiveness.ALIVE,
        projectRoot: '/projects/serve',
        head: {
          strategy: 'standard',
          lifecycle: {state: {case: 'running', value: {}}},
          objective: {
            series: {
              name: 'Throughput',
              unit: 'requests/s',
              direction: Direction.MAX,
              role: SeriesRole.OBJECTIVE,
            },
            bestValue: 87.5,
          },
          usage: {costUsd: 4.25},
          needsUser: true,
        },
      }),
      runsSyncedFrame,
      {hang: true},
    ])(selectedRecording)
  : selectedRecording;
const fake = makeFakeTransport(recording);
const saved = savedAppearance(query);
const preferences = makeFakePreferencesBridge(saved === undefined ? {} : {saved});

const root = document.getElementById('root');
if (root === null) throw new Error('fixture.html has no #root');
const page = createRoot(root);
Effect.runFork(
  Effect.gen(function* () {
    const scope = yield* Scope.make();
    const appearance = yield* makeAppearance().pipe(Scope.provide(scope));
    const navigation = yield* makeNavigation().pipe(Scope.provide(scope));
    const viewer = (yield* (yield* Viewer).whoami).id;
    const folders = yield* makeMachineFolders(memoryFolders().actions);
    const hosts: ConnectedHost[] = [];
    for (const {key, label} of shown) {
      const session = yield* makeHostSession(key).pipe(Scope.provide(scope));
      hosts.push({key, label, session});
    }
    page.render(
      <StrictMode>
        <DisplayClockProvider zone="America/Los_Angeles">
          <ConnectedShell
            hosts={hosts}
            navigation={navigation}
            appearance={appearance}
            viewerId={viewer}
            folders={folders}
            machines={fixtureMachineActions()}
            dispatch={(effect, then) => {
              Effect.runFork(Effect.map(effect, then));
            }}
          />
        </DisplayClockProvider>
      </StrictMode>,
    );
    yield* Effect.never;
  }).pipe(
    Effect.provide(
      Layer.mergeAll(
        fake.layer,
        Ids.layer,
        preferencesOver(preferences.bridge),
        viewerOver(makeFakeViewerBridge({viewer: FAKE_VIEWER}).bridge),
        SystemAppearanceFromWindow,
        ThemeDocumentLive,
        KeyPressesFromWindow,
      ),
    ),
    Effect.scoped,
    Effect.provideService(Clock.Clock, frozenClock),
  ),
);
