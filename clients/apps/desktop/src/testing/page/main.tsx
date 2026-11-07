/**
 * The window over a fake daemon, as a browser page: for looking at the screens and driving them
 * with Playwright without Electron. Built by `vite build --mode fixture` into `dist/fixture`.
 * `?scenario=<seed>-<subagents>-<live|done>[-tree[-<depth>]]` picks the run (`tree` has agents spawn
 * each other and uses every kind of tool call; `depth` makes histories longer); one `host=<key>` per
 * host to show (this machine when none); `appearance=<mode>-<light theme>-<dark theme>` sets what is
 * saved. Saved appearance lives in memory, so a reload starts again from the address.
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
import {FakeDaemon, generateScenario} from '../orchestrator/index.js';
import {daemonRecording} from '../orchestrator/recordings.js';
import {makeFakePreferencesBridge} from '../settings/fakes.js';
import {makeFakeTransport} from '../transport/index.js';

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

const query = new URLSearchParams(window.location.search);
const daemon = new FakeDaemon({
  ...scenarioOf(window.location.search),
  search: true,
  searchStale: query.has('stale'),
  searchTruncated: query.has('truncated'),
});
const fake = makeFakeTransport(
  query.has('login-failed')
    ? {
        respond: () => [
          {end: {linkProblem: DesktopLinkProblem.AUTH, message: 'Permission denied (publickey).'}},
        ],
      }
    : daemonRecording(daemon),
);
const saved = savedAppearance(query);
const preferences = makeFakePreferencesBridge(saved === undefined ? {} : {saved});
const named = hostsOfAddress(window.location.search);
const shown = named.length === 0 ? [THIS_MACHINE] : named;

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
