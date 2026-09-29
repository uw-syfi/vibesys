import type {DesignRound, HypothesisEntry, PerformanceRound} from '@vibesys/backend-client';
import {hasRunEnded} from '@vibesys/core-state';
import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import {
  agentGraph,
  endedWord,
  headerModel,
  inspectorModel,
  latestRound,
  logGroups,
  needsOlder,
  railModel,
  railState,
  showsChanges,
  steers,
  steersNeedOlder,
  summaryModel,
  toolOutput,
  trendModel,
} from './derive.js';
import type {WorkspaceSession} from './session.js';
import {Banner} from './ui/Banner.js';
import {Composer} from './ui/Composer.js';
import {Graph} from './ui/Graph.js';
import {Header} from './ui/Header.js';
import {Inspector} from './ui/Inspector.js';
import {LiveRegion} from './ui/LiveRegion.js';
import {Log, type LogHandle} from './ui/Log.js';
import {Rail} from './ui/Rail.js';
import {Shortcuts} from './ui/Shortcuts.js';
import {Summary} from './ui/Summary.js';
import {Tooltip} from './ui/Tooltip.js';
import './App.css';

const WIDE = '(min-width: 1200px)';
const TABLET = '(min-width: 768px)';
const HINT_KEY = 'vibesys.web.sheet-hint';
const NO_EXPERIMENTS: HypothesisEntry[] = [];
const NO_DESIGN: DesignRound[] = [];
const NO_PERFORMANCE: PerformanceRound[] = [];
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

/**
 * Hands the log focus that is nowhere a reader can use it: on `<body>`, or on an element that
 * has been hidden under it. A reader left there has arrow keys that move the round instead of
 * the log's cursor, which is the whole of it. Focus that is somewhere real is left alone.
 */
function reclaimFocus(): void {
  const at = document.activeElement;
  if (!(at instanceof HTMLElement) || at === document.body || !at.checkVisibility()) {
    document.getElementById('log')?.focus({preventScroll: true});
  }
}

function hintSeen(): boolean {
  try {
    return localStorage.getItem(HINT_KEY) === 'seen';
  } catch {
    return false;
  }
}

/** `connect` renders under the header: the gateway form on a page with no live gateway. */
export function App({session, connect}: {session: WorkspaceSession; connect?: ReactNode}) {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const wide = useMedia(WIDE);
  const tablet = useMedia(TABLET);
  const [picked, setPicked] = useState<{runId: string | null; round: number} | null>(null);
  // The row whose output the inspector shows, tied to the round it was picked in so that
  // changing round clears it without an effect.
  const [rowPick, setRowPick] = useState<{round: number; id: string} | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [hinted, setHinted] = useState(hintSeen);
  const log = useRef<LogHandle>(null);

  const {core, captured, runId, connection, queries, command} = state;
  const experiments = queries.experiments.response?.experiments ?? NO_EXPERIMENTS;
  const design = queries.design.response?.design ?? NO_DESIGN;
  const context = queries.performance.response?.performance_context ?? null;
  const series = queries.performance.response?.performance ?? NO_PERFORMANCE;
  const rail = useMemo(() => railModel(core, experiments, context), [core, experiments, context]);
  const trend = useMemo(() => trendModel(series, context), [series, context]);
  const summary = useMemo(
    () => summaryModel(rail, context, core.maxRounds, experiments),
    [rail, context, core.maxRounds, experiments],
  );
  const steer = useMemo(() => steers(captured), [captured]);
  const live = latestRound(core);
  // The live round stays selected on new rounds until the user picks another.
  const selected = picked !== null && picked.runId === runId ? picked.round : live;
  const groups = useMemo(
    () => (selected === null ? [] : logGroups(core, steer.consumed, selected, runId)),
    [core, steer, selected, runId],
  );
  const graph = useMemo(() => agentGraph(core, selected), [core, selected]);
  const inspector = useMemo(
    () =>
      selected === null
        ? null
        : inspectorModel(rail.rows, experiments, design, captured, selected, context),
    [rail, experiments, design, captured, selected, context],
  );
  // The guard covers the live round advancing on its own; `select` and `step` clear the pick
  // outright, so a round the reader comes back to does not restore the output it once showed.
  const selectedRow = rowPick !== null && rowPick.round === selected ? rowPick.id : null;
  const output = useMemo(() => toolOutput(core, selectedRow), [core, selectedRow]);
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

  // Past 1024px the inspector is an aside, so an open dialog is removed rather than closed, and
  // removing an open <dialog> fires no `close` at all: the hand-off below never runs and focus
  // falls to <body>. A reader at 125% zoom is in the drawer layout, and one Cmd+0 crosses this.
  // Keyed on the width rather than on the dialog's own teardown, which would also fire on an
  // ordinary close: there the dialog has already restored focus synchronously inside `close()`,
  // so the guard would find a visible row and take nothing. Firing it on a path with no focus
  // to reclaim is the wrong place for it, not a theft.
  useEffect(() => {
    if (wide && inspectorOpen) reclaimFocus();
  }, [wide, inspectorOpen]);

  function select(round: number) {
    const again = round === selected;
    setPicked(round === live ? null : {runId, round});
    if (!again) setRowPick(null);
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

  function selectRow(id: string | null) {
    setRowPick(id === null || selected === null ? null : {round: selected, id});
    // Narrow: the inspector is a dialog, so a row picked there has to open it or nothing happens.
    if (id !== null && !wide) setInspectorOpen(true);
  }

  function step(offset: number) {
    const index = rail.rows.findIndex(row => row.round === selected);
    const next = rail.rows[Math.min(rail.rows.length - 1, Math.max(0, index + offset))];
    if (next === undefined || next.round === selected) return;
    setPicked(next.round === live ? null : {runId, round: next.round});
    setRowPick(null);
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
      {connect}
      <Banner
        connection={connection}
        connectionError={state.connectionError}
        canRetry={state.canRetry}
        snapshotError={state.snapshotError}
        onReconnect={() => {
          // Retry unmounts the banner; focus goes to the log instead of falling to <body>.
          document.getElementById('log')?.focus({preventScroll: true});
          void session.reconnect();
        }}
        onRetrySnapshot={() => void session.refresh()}
      />
      {summary === null ? null : <Summary model={summary} trend={trend} />}
      <div className="shell">
        <Rail
          state={railState(
            queries.experiments.response,
            queries.experiments.error,
            hasRunEnded(core),
          )}
          model={rail}
          selected={selected}
          error={queries.experiments.error}
          hint={!tablet && !hinted}
          onSelect={select}
          onRetry={() => void session.load('experiments')}
        />
        <main className="center">
          <Graph round={selected} graph={graph} />
          <Log
            key={selected ?? 'none'}
            ref={log}
            // Loading until the bootstrap batch folds an event or the snapshot gives a status:
            // `subscribed` sets runId a frame before the batch arrives.
            state={core.status === 'connecting' && core.sequence === 0 ? 'loading' : 'ready'}
            round={selected}
            groups={groups}
            follow={selected !== null && selected === live}
            // Backfill state belongs to the rounds that asked for it, not to every round.
            history={{
              loading: wantsHistory && historyLoading,
              error: wantsHistory ? historyError : null,
              onRetry: () => void session.loadOlder(),
            }}
            selected={selectedRow}
            onSelect={selectRow}
          />
          {ended === null ? (
            <Composer
              pending={steer.pending}
              disabled={connection !== 'connected'}
              error={error?.action === 'steer' ? commandError : null}
              onSend={text => session.command({type: 'command.steer', text})}
            />
          ) : null}
        </main>
        <Inspector
          model={inspector}
          output={output}
          mode={wide ? 'aside' : tablet ? 'drawer' : 'sheet'}
          open={inspectorOpen}
          judgePending={roundHidden && historyLoading}
          designError={
            showsChanges(rail.rows.find(row => row.round === selected))
              ? queries.design.error
              : null
          }
          onClose={() => {
            setInspectorOpen(false);
            // A modal <dialog> restores focus to the node it remembered on open, and a commit
            // behind it can have replaced that row. Focus then stays somewhere the reader
            // cannot use: on <body>, or on the dialog's own control now that the dialog is
            // hidden. No React commit attends the close, so the log cannot see it for itself.
            reclaimFocus();
          }}
          onRetryDesign={() => void session.load('design')}
        />
      </div>
      <LiveRegion status={core.status} round={live} ended={ended} connection={connection} />
      <Shortcuts
        onNext={() => step(1)}
        onPrevious={() => step(-1)}
        onToggleRun={toggleRun}
        onJumpToLive={() => log.current?.jump()}
      />
      <Tooltip />
    </div>
  );
}
