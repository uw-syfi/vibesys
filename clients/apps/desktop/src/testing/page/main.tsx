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
 * `report` has the lead post a report about the whole run, which the report screen sums the run up by
 * (a Catch me up answer, a window report, never replaces it);
 * `catchUp=<none|history|writing|written|pending|failed>` gives the viewer check-ins already made
 * on the run (none when absent), and a press of Catch me up makes another that stays pending while
 * the lead writes; `measured=<candidates>` gives the run that many measured candidates on two
 * objectives, with the lead's bounds, so the report has its charts and tiles (the head is the best
 * of the first); `appearance=<mode>-<light theme>-<dark theme>` sets what is saved. Saved appearance lives in
 * memory, so a reload starts again from the address.
 */
import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
import {ReportKind} from '@vibesys/orchestrator-proto';
import {DesktopLinkProblem} from '@vibesys/proto';
import {type Appearance, parseAppearance} from '@vibesys/ui/tokens';
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
  makeNavigation,
  preferencesOver,
  Viewer,
  viewerOver,
} from '../../core/index.js';
import type {CostView} from '../../state/index.js';
import {hostsOfAddress} from '../../ui/address.js';
import type {ConnectedHost} from '../../ui/orchestrator/connected.js';
import {
  KeyPressesFromWindow,
  SystemAppearanceFromWindow,
  ThemeDocumentLive,
} from '../../ui/shell/browser-layers.js';
import {ConnectedShell} from '../../ui/shell/connected.js';
import {CATCH_UP_SHAPES, seedCheckIns} from '../orchestrator/check-in-fixtures.js';
import {
  ENDED_RUN,
  LONG_NAMES,
  NO_PRICES,
  NO_TURNS,
  PRICED_AND_UNPRICED,
} from '../orchestrator/cost-views.js';
import {
  FAKE_VIEWER,
  FakeDaemon,
  fleetScenario,
  generateScenario,
  measuredSearch,
  methodPathOf,
  SERVING,
  START_MS,
  withReport,
} from '../orchestrator/index.js';
import {daemonRecording} from '../orchestrator/recordings.js';
import {servedCost} from '../orchestrator/served-cost.js';
import {makeFakePreferencesBridge} from '../settings/fakes.js';
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
const named = hostsOfAddress(window.location.search);
const shown = named.length === 0 ? [THIS_MACHINE] : named;

/** The host at position `down` loses its link after its first snapshot: its run stream ends. */
const downAt = query.has('down') ? Number(query.get('down')) : undefined;

/** Each host's own fleet, from `fleet=<runs>-<live>`: the same shape, a different seed. */
function fleetRecording(spec: string): Recording {
  const [runs = '12', live = '4'] = spec.split('-');
  const nowMs = Date.now();
  const leftOut = Number(query.get('leftOut') ?? 0);
  const bases = new Map(
    shown.map((host, index) => {
      const daemon = new FakeDaemon(
        fleetScenario({
          seed: 11 + index,
          prefix: `h${index}`,
          runs: Number(runs),
          live: Number(live),
          nowMs,
          ...(query.has('long') ? {longEvery: 3} : {}),
        }),
      );
      daemon.runsLeftOut = leftOut;
      return [host.key, daemonRecording(daemon)] as const;
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

const base = scenarioOf(window.location.search);
const scenario = query.has('report')
  ? withReport(base, {
      sequence: 1,
      kind: ReportKind.PROGRESS,
      atMs: base.endMs - 20 * 60_000,
      headline: 'Throughput is 2.3 times the baseline at p99 118 ms.',
      changed: 'Fused expert dispatch landed.',
      openForUser: 'Should quantization stay paused?',
    })
  : base;
const measured = query.get('measured');
const daemon = new FakeDaemon({
  ...scenario,
  search: true,
  searchStale: query.has('stale'),
  searchTruncated: query.has('truncated'),
  ...(measured === null
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
            {stale: query.has('stale')},
          ),
        ],
      }),
});
const cost = COSTS[query.get('cost') ?? 'priced'];
if (cost !== undefined) daemon.publishCost(servedCost(cost, scenario.runId));
const shape = query.get('catchUp') ?? 'none';
seedCheckIns(daemon, scenario, CATCH_UP_SHAPES.find(each => each === shape) ?? 'none');
const served = daemonRecording(daemon, {live: true});
/** A host that predates the cost stream answers every other method and not this one. */
const withoutCostStream = {
  respond: (...call: Parameters<typeof served.respond>) =>
    call[0].method === methodPathOf('WatchRunCost')
      ? [{end: {message: 'no such method', status: RpcStatus.UNIMPLEMENTED}}]
      : served.respond(...call),
};
const fleetSpec = query.get('fleet');
const fake = makeFakeTransport(
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
        : served,
);
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
    const hosts: ConnectedHost[] = [];
    for (const {key, label} of shown) {
      const session = yield* makeHostSession(key).pipe(Scope.provide(scope));
      hosts.push({key, label, session});
    }
    page.render(
      <StrictMode>
        <ConnectedShell
          hosts={hosts}
          navigation={navigation}
          appearance={appearance}
          viewerId={viewer}
          dispatch={(effect, then) => {
            Effect.runFork(Effect.map(effect, then));
          }}
        />
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
  ),
);
