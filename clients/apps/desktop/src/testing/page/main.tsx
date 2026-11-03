/**
 * The orchestrator window over a fake daemon, as a browser page: for looking at the screens and
 * driving them with Playwright without Electron. Built by `vite build --mode fixture` into
 * `dist/fixture`; `?scenario=<seed>-<subagents>-<live|done>` picks the run.
 */
import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
import {DesktopLinkProblem} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {Ids, makeHostSession} from '../../core/index.js';
import {ConnectedApp} from '../../ui/orchestrator/connected.js';
import {FakeDaemon, generateScenario} from '../orchestrator/index.js';
import {daemonRecording} from '../orchestrator/recordings.js';
import {makeFakeTransport} from '../transport/index.js';

const DEFAULT_SCENARIO = '5-4-live';

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

const root = document.getElementById('root');
if (root === null) throw new Error('fixture.html has no #root');
const page = createRoot(root);
Effect.runFork(
  makeHostSession('local').pipe(
    Effect.provide(fake.layer),
    Effect.provide(Ids.layer),
    Effect.flatMap(session =>
      Effect.andThen(
        Effect.sync(() =>
          page.render(
            <StrictMode>
              <ConnectedApp
                hosts={[{key: 'local', label: 'This machine', session}]}
                dispatch={(effect, then) => {
                  Effect.runFork(Effect.map(effect, then));
                }}
              />
            </StrictMode>,
          ),
        ),
        Effect.never,
      ),
    ),
    Effect.scoped,
  ),
);
