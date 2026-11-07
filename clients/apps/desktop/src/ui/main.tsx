/**
 * The renderer entry (`dom` runtime), one of the two files that runs Effect programs. It builds the
 * services over the window's bridges, shows the saved theme, opens one session per host the address
 * names, and renders the window over them.
 */
import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import * as Scope from 'effect/Scope';
import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {
  GatewayPortsFromWindow,
  GatewayTransportLive,
  Ids,
  makeAppearance,
  makeHostSession,
  makeNavigation,
  PreferencesFromWindow,
} from '../core/index.js';
import {hostsOfAddress} from './address.js';
import type {ConnectedHost, Dispatch} from './orchestrator/connected.js';
import {
  KeyPressesFromWindow,
  SystemAppearanceFromWindow,
  ThemeDocumentLive,
} from './shell/browser-layers.js';
import {ConnectedShell} from './shell/connected.js';

const dispatch: Dispatch = (effect, then) => {
  Effect.runFork(Effect.map(effect, then));
};

const open = Effect.gen(function* () {
  const scope = yield* Scope.make();
  // The theme first, so the page is never drawn in one the person did not choose.
  const appearance = yield* makeAppearance().pipe(Scope.provide(scope));
  const navigation = yield* makeNavigation().pipe(Scope.provide(scope));
  const sessions: ConnectedHost[] = [];
  for (const {key, label} of hostsOfAddress(window.location.search)) {
    const session = yield* makeHostSession(key).pipe(Scope.provide(scope));
    sessions.push({key, label, session});
  }
  return {sessions, appearance, navigation};
}).pipe(
  Effect.provide(
    Layer.mergeAll(
      GatewayTransportLive,
      Ids.layer,
      PreferencesFromWindow,
      SystemAppearanceFromWindow,
      ThemeDocumentLive,
      KeyPressesFromWindow,
    ).pipe(Layer.provide(GatewayPortsFromWindow)),
  ),
);

const root = document.getElementById('root');
if (root === null) throw new Error('index.html has no #root');
const page = createRoot(root);
Effect.runFork(
  Effect.map(open, ({sessions, appearance, navigation}) => {
    page.render(
      <StrictMode>
        <ConnectedShell
          hosts={sessions}
          dispatch={dispatch}
          navigation={navigation}
          appearance={appearance}
        />
      </StrictMode>,
    );
  }),
);
