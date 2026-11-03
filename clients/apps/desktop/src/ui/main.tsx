/**
 * The renderer entry (`dom` runtime), one of the two files that runs Effect programs. It builds the
 * services over the window's gateway bridge, opens one session per host the address names, and
 * renders the orchestrator window over them.
 */
import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import * as Scope from 'effect/Scope';
import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {GatewayPortsFromWindow, GatewayTransportLive, Ids, makeHostSession} from '../core/index.js';
import {hostsOfAddress} from './address.js';
import {ConnectedApp, type ConnectedHost, type Dispatch} from './orchestrator/connected.js';

const dispatch: Dispatch = (effect, then) => {
  Effect.runFork(Effect.map(effect, then));
};

const openSessions = Effect.gen(function* () {
  const scope = yield* Scope.make();
  const sessions: ConnectedHost[] = [];
  for (const {key, label} of hostsOfAddress(window.location.search)) {
    const session = yield* makeHostSession(key).pipe(Scope.provide(scope));
    sessions.push({key, label, session});
  }
  return sessions;
}).pipe(
  Effect.provide(
    Layer.mergeAll(GatewayTransportLive, Ids.layer).pipe(Layer.provide(GatewayPortsFromWindow)),
  ),
);

const root = document.getElementById('root');
if (root === null) throw new Error('index.html has no #root');
const page = createRoot(root);
Effect.runFork(
  Effect.map(openSessions, hosts => {
    page.render(
      <StrictMode>
        <ConnectedApp hosts={hosts} dispatch={dispatch} />
      </StrictMode>,
    );
  }),
);
