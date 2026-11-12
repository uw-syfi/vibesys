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
  type MachineFolders,
  Machines,
  MachinesFromWindow,
  makeAppearance,
  makeMachineActions,
  makeMachineFolders,
  makeNavigation,
  makeRoster,
  type NavigationStore,
  PreferencesFromWindow,
  type Roster,
  Viewer,
  ViewerFromWindow,
  type ViewerIdentity,
} from '../core/index.js';
import {hostsOfAddress} from './address.js';
import {DisplayClockProvider} from './display-clock.js';
import {systemDisplayZone} from './display-time.js';
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
    Effect.catch(error =>
      Effect.logWarning(`could not read the viewer: ${error.detail}`).pipe(Effect.as(null)),
    ),
  );
  // The hosts the address names, and the ones the person adds or removes while the window is open.
  const roster = yield* makeRoster(
    hostsOfAddress(window.location.search).map(host => host.key),
  ).pipe(Scope.provide(scope));
  const plain = makeMachineActions(yield* Machines, roster);
  const folders = yield* makeMachineFolders(plain);
  yield* folders.load;
  const machines = folders.follow(plain);
  return {roster, machines, folders, appearance, navigation, viewer};
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
  readonly folders: MachineFolders;
  readonly navigation: NavigationStore;
  readonly appearance: AppearanceStore;
  readonly viewer: ViewerIdentity | null;
}): JSX.Element {
  const hosts = useAtomValue(props.roster.entries);
  return (
    <ConnectedShell
      hosts={hosts}
      dispatch={dispatch}
      navigation={props.navigation}
      appearance={props.appearance}
      viewer={props.viewer}
      machines={props.machines}
      folders={props.folders}
    />
  );
}

const root = document.getElementById('root');
if (root === null) throw new Error('index.html has no #root');
const page = createRoot(root);
// The display zone is the system's, read once here and passed in.
const zone = systemDisplayZone();
Effect.runFork(
  Effect.map(open, ({roster, machines, folders, appearance, navigation, viewer}) => {
    page.render(
      <StrictMode>
        <DisplayClockProvider zone={zone}>
          <Window
            roster={roster}
            machines={machines}
            folders={folders}
            navigation={navigation}
            appearance={appearance}
            viewer={viewer}
          />
        </DisplayClockProvider>
      </StrictMode>,
    );
  }),
);
