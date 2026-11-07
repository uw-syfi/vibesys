/**
 * The window over a fake daemon, as a browser page: for looking at the screens and driving them
 * with Playwright without Electron. Built by `vite build --mode fixture` into `dist/fixture`.
 * `?scenario=<seed>-<subagents>-<live|done>` picks the run; one `host=<key>` per host to show
 * (this machine when none); `fleet=<runs>-<live>` gives each host that many runs instead (`long`
 * names every third run very long, `down=<n>` has the host at that position lose its link after
 * its first snapshot); `appearance=<mode>-<light theme>-<dark theme>` sets what is saved.
 * Saved appearance lives in memory, so a reload starts again from the address.
 */
import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
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
} from '../../core/index.js';
import {hostsOfAddress} from '../../ui/address.js';
import type {ConnectedHost} from '../../ui/orchestrator/connected.js';
import {
  KeyPressesFromWindow,
  SystemAppearanceFromWindow,
  ThemeDocumentLive,
} from '../../ui/shell/browser-layers.js';
import {ConnectedShell} from '../../ui/shell/connected.js';
import {FakeDaemon, fleetScenario, generateScenario} from '../orchestrator/index.js';
import {daemonRecording, methodPathOf} from '../orchestrator/recordings.js';
import {makeFakePreferencesBridge} from '../settings/fakes.js';
import {makeFakeTransport, type Recording} from '../transport/index.js';

const DEFAULT_SCENARIO = '5-4-live';
const THIS_MACHINE = {key: 'local', label: 'This machine'} as const;

function scenarioOf(search: string) {
  const [seed = '5', subagents = '4', mode = 'live'] = (
    new URLSearchParams(search).get('scenario') ?? DEFAULT_SCENARIO
  ).split('-');
  return generateScenario({
    seed: Number(seed),
    subagents: Number(subagents),
    live: mode === 'live',
  });
}

/** The appearance the address says is saved, if it says one. */
function savedAppearance(query: URLSearchParams): Appearance | undefined {
  const given = query.get('appearance');
  if (given === null) return undefined;
  const [mode, light, dark] = given.split('-');
  return parseAppearance({mode, light, dark}, 'appearance');
}

const query = new URLSearchParams(window.location.search);
const named = hostsOfAddress(window.location.search);
const shown = named.length === 0 ? [THIS_MACHINE] : named;

/** The host at position `down` loses its link after its first snapshot: its run stream ends. */
const downAt = query.has('down') ? Number(query.get('down')) : undefined;

/** Each host's own fleet, from `fleet=<runs>-<live>`: the same shape, a different seed. */
function fleetRecording(spec: string): Recording {
  const [runs = '12', live = '4'] = spec.split('-');
  const nowMs = Date.now();
  const bases = new Map(
    shown.map((host, index) => [
      host.key,
      daemonRecording(
        new FakeDaemon(
          fleetScenario({
            seed: 11 + index,
            prefix: `h${index}`,
            runs: Number(runs),
            live: Number(live),
            nowMs,
            ...(query.has('long') ? {longEvery: 3} : {}),
          }),
        ),
      ),
    ]),
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

const daemon = new FakeDaemon({
  ...scenarioOf(window.location.search),
  search: true,
  searchStale: query.has('stale'),
  searchTruncated: query.has('truncated'),
});
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
      : daemonRecording(daemon),
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
        SystemAppearanceFromWindow,
        ThemeDocumentLive,
        KeyPressesFromWindow,
      ),
    ),
    Effect.scoped,
  ),
);
