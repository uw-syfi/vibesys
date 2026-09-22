import {useSyncExternalStore} from 'react';
import type {HeaderModel, RailModel} from './model.js';
import type {WorkspaceSession} from './session.js';
import {Banner} from './ui/Banner.js';
import {Composer} from './ui/Composer.js';
import {Header} from './ui/Header.js';
import {Inspector} from './ui/Inspector.js';
import {LiveRegion} from './ui/LiveRegion.js';
import {Log} from './ui/Log.js';
import {Rail} from './ui/Rail.js';
import {Shortcuts} from './ui/Shortcuts.js';
import {Tooltip} from './ui/Tooltip.js';
import './App.css';

// Scaffold: task 4 replaces these constants with the store and derivations.
const HEADER: HeaderModel = {
  project: null,
  objective: null,
  startedAt: null,
  endedAt: null,
  control: {
    kind: 'action',
    action: 'pause',
    label: 'Pause',
    tip: 'Pause after the current agent call',
    disabled: true,
  },
};
const RAIL: RailModel = {rows: [], roundsLeft: null};
const HISTORY = {loading: false, error: null, onRetry: () => {}};
const noop = () => {};

export function App({session}: {session: WorkspaceSession}) {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  return (
    <div className="app">
      <a className="skip" href="#log">
        Skip to log
      </a>
      <Header model={HEADER} error={null} onControl={noop} />
      <Banner
        connection={state.connection}
        connectionError={state.connectionError}
        canRetry={false}
        snapshotError={state.snapshotError}
        onReconnect={noop}
        onRetrySnapshot={noop}
      />
      <div className="shell">
        <Rail
          state="loading"
          model={RAIL}
          selected={null}
          error={null}
          hint={false}
          onSelect={noop}
          onRetry={noop}
        />
        <div className="center">
          <Log state="loading" round={null} groups={[]} follow={false} history={HISTORY} />
          <Composer pending={[]} disabled error={null} onSend={async () => false} />
        </div>
        <Inspector
          model={null}
          mode="aside"
          open={false}
          designError={null}
          onClose={noop}
          onRetryDesign={noop}
        />
      </div>
      <LiveRegion status={state.core.status} round={null} ended={null} />
      <Shortcuts onNext={noop} onPrevious={noop} onToggleRun={noop} />
      <Tooltip />
    </div>
  );
}
