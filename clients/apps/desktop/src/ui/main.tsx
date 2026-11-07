/**
 * The renderer entry (`dom` runtime), one of the two files that runs Effect programs. It builds the
 * services over the window's bridges, shows the saved theme, opens one session per host the address
 * names, and renders the window over them.
 */
import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
import {useAtomValue} from '@effect/atom-react';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import * as Scope from 'effect/Scope';
import {type JSX, StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {
  type AppearanceStore,
  GatewayPortsFromWindow,
  GatewayTransportLive,
  Ids,
  type MachineActions,
  Machines,
  MachinesFromWindow,
  makeAppearance,
  makeMachineActions,
  makeNavigation,
  makeRoster,
  type NavigationStore,
  PreferencesFromWindow,
  type Roster,
  Viewer,
  ViewerFromWindow,
} from '../core/index.js';
import {hostsOfAddress} from './address.js';
import type {Dispatch} from './orchestrator/connected.js';
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
  // Who this install is, to tell which check-ins a host keeps are its own; the main process stamps it
  // on every call, so an app that cannot read it still works and just lists none as its own.
  const viewer = yield* (yield* Viewer).whoami.pipe(
    Effect.map(identity => identity.id),
    Effect.catch(error =>
      Effect.logWarning(`could not read the viewer: ${error.detail}`).pipe(Effect.as(null)),
    ),
  );
  // The hosts the address names, and the ones the person adds or removes while the window is open.
  const roster = yield* makeRoster(
    hostsOfAddress(window.location.search).map(host => host.key),
  ).pipe(Scope.provide(scope));
  const machines = makeMachineActions(yield* Machines, roster);
  return {roster, machines, appearance, navigation, viewer};
}).pipe(
  Effect.provide(
    Layer.mergeAll(
      GatewayTransportLive,
      Ids.layer,
      PreferencesFromWindow,
      ViewerFromWindow,
      SystemAppearanceFromWindow,
      ThemeDocumentLive,
      KeyPressesFromWindow,
      MachinesFromWindow,
    ).pipe(Layer.provide(GatewayPortsFromWindow)),
  ),
);

/** The window over the hosts as they stand: a host added or removed changes the list it is given. */
function Window(props: {
  readonly roster: Roster;
  readonly machines: MachineActions;
  readonly navigation: NavigationStore;
  readonly appearance: AppearanceStore;
  readonly viewerId: string | null;
}): JSX.Element {
  const hosts = useAtomValue(props.roster.entries);
  return (
    <ConnectedShell
      hosts={hosts}
      dispatch={dispatch}
      navigation={props.navigation}
      appearance={props.appearance}
      viewerId={props.viewerId}
      machines={props.machines}
    />
  );
}

const root = document.getElementById('root');
if (root === null) throw new Error('index.html has no #root');
const page = createRoot(root);
Effect.runFork(
  Effect.map(open, ({roster, machines, appearance, navigation, viewer}) => {
    page.render(
      <StrictMode>
        <Window
          roster={roster}
          machines={machines}
          navigation={navigation}
          appearance={appearance}
          viewerId={viewer}
        />
      </StrictMode>,
    );
  }),
);
