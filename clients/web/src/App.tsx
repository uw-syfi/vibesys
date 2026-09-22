import type {DesignRound, HypothesisEntry} from '@vibesys/backend-client/browser';
import {useCallback, useEffect, useMemo, useState, useSyncExternalStore} from 'react';
import {
  endedWord,
  headerModel,
  inspectorModel,
  latestRound,
  logGroups,
  needsOlder,
  railModel,
  showsChanges,
  steers,
  steersNeedOlder,
} from './derive.js';
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

const WIDE = '(min-width: 1024px)';
const TABLET = '(min-width: 768px)';
const HINT_KEY = 'vibesys.web.sheet-hint';
const NO_EXPERIMENTS: HypothesisEntry[] = [];
const NO_DESIGN: DesignRound[] = [];
const ACTIONS = {pause: 'Pause', resume: 'Resume', steer: 'Steer'} as const;

function useMedia(query: string): boolean {
  const subscribe = useCallback(
    (notify: () => void) => {
      const media = matchMedia(query);
      media.addEventListener('change', notify);
      return () => media.removeEventListener('change', notify);
    },
    [query],
  );
  return useSyncExternalStore(subscribe, () => matchMedia(query).matches);
}

function hintSeen(): boolean {
  try {
    return localStorage.getItem(HINT_KEY) === 'seen';
  } catch {
    return false;
  }
}

export function App({session}: {session: WorkspaceSession}) {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const wide = useMedia(WIDE);
  const tablet = useMedia(TABLET);
  const [picked, setPicked] = useState<{runId: string | null; round: number} | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [hinted, setHinted] = useState(hintSeen);

  const {core, captured, runId, connection, queries, command} = state;
  const experiments = queries.experiments.response?.experiments ?? NO_EXPERIMENTS;
  const design = queries.design.response?.design ?? NO_DESIGN;
  const context = queries.performance.response?.performance_context ?? null;
  const rail = useMemo(() => railModel(core, experiments, context), [core, experiments, context]);
  const steer = useMemo(() => steers(captured), [captured]);
  const live = latestRound(core);
  // The live round stays selected on new rounds until the user picks another.
  const selected = picked !== null && picked.runId === runId ? picked.round : live;
  const groups = useMemo(
    () => (selected === null ? [] : logGroups(core, steer.consumed, selected, runId)),
    [core, steer, selected, runId],
  );
  const inspector = useMemo(
    () =>
      selected === null
        ? null
        : inspectorModel(rail.rows, experiments, design, captured, selected, context),
    [rail, experiments, design, captured, selected, context],
  );
  const header = headerModel(core, captured, connection, context);
  const ended = endedWord(core, captured);
  const error = command.error;

  // Backfill while the tail floor may hide the selected round's events (verdicts included) or
  // the text of a steer it consumed: one 500-event chunk at a time (single-flight in the
  // session), re-evaluated after each, never retried after an error. Every folded chunk lowers
  // the floor, so no floor is requested twice within a bootstrap; a bootstrap that orphans a
  // chunk clears the loading flag and requests at its own floor.
  const roundHidden = selected !== null && needsOlder(core, selected);
  const wantsHistory =
    roundHidden || (selected !== null && steersNeedOlder(core, captured, selected));
  const {historyLoading, historyError} = state;
  useEffect(() => {
    if (wantsHistory && !historyLoading && historyError === null) void session.loadOlder();
  }, [session, wantsHistory, historyLoading, historyError]);
  const commandError = error === null ? null : `${ACTIONS[error.action]} failed: ${error.message}`;

  function select(round: number) {
    const again = round === selected;
    setPicked(round === live ? null : {runId, round});
    if (wide || (!tablet && !again)) return;
    setInspectorOpen(true);
    if (!tablet && !hinted) {
      setHinted(true);
      try {
        localStorage.setItem(HINT_KEY, 'seen');
      } catch {
        // Private mode: the hint shows again next visit.
      }
    }
  }

  function step(offset: number) {
    const index = rail.rows.findIndex(row => row.round === selected);
    const next = rail.rows[Math.min(rail.rows.length - 1, Math.max(0, index + offset))];
    if (next !== undefined) setPicked(next.round === live ? null : {runId, round: next.round});
  }

  function toggleRun() {
    const control = header.control;
    if (control.kind !== 'action' || control.disabled) return;
    void session.command(
      control.action === 'pause'
        ? {type: 'command.pause', mode: 'after_current_agent_call'}
        : {type: 'command.resume'},
    );
  }

  const experimentsResponse = queries.experiments.response;
  const railState =
    experimentsResponse === null
      ? 'loading'
      : experimentsResponse.experiments_ready === false
        ? 'unattached'
        : 'ready';

  return (
    <div className="app">
      <a className="skip" href="#log">
        Skip to log
      </a>
      <Header
        model={header}
        error={error !== null && error.action !== 'steer' ? commandError : null}
        onControl={toggleRun}
      />
      <Banner
        connection={connection}
        connectionError={state.connectionError}
        canRetry={state.canRetry}
        snapshotError={state.snapshotError}
        onReconnect={() => void session.reconnect()}
        onRetrySnapshot={() => void session.refresh()}
      />
      <div className="shell">
        <Rail
          state={railState}
          model={rail}
          selected={selected}
          error={queries.experiments.error}
          hint={!tablet && !hinted}
          onSelect={select}
          onRetry={() => void session.load('experiments')}
        />
        <div className="center">
          <Log
            key={selected ?? 'none'}
            state={runId === null ? 'loading' : 'ready'}
            round={selected}
            groups={groups}
            follow={selected !== null && selected === live}
            history={{
              loading: historyLoading,
              error: historyError,
              onRetry: () => void session.loadOlder(),
            }}
          />
          {ended === null ? (
            <Composer
              pending={steer.pending}
              disabled={connection !== 'connected'}
              error={error?.action === 'steer' ? commandError : null}
              onSend={text => session.command({type: 'command.steer', text})}
            />
          ) : null}
        </div>
        <Inspector
          model={inspector}
          mode={wide ? 'aside' : tablet ? 'drawer' : 'sheet'}
          open={inspectorOpen}
          judgePending={roundHidden && historyLoading}
          designError={
            showsChanges(rail.rows.find(row => row.round === selected))
              ? queries.design.error
              : null
          }
          onClose={() => setInspectorOpen(false)}
          onRetryDesign={() => void session.load('design')}
        />
      </div>
      <LiveRegion status={core.status} round={live} ended={ended} />
      <Shortcuts onNext={() => step(1)} onPrevious={() => step(-1)} onToggleRun={toggleRun} />
      <Tooltip />
    </div>
  );
}
