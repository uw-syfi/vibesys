/** The run window over one WorkspaceSession: the only component that reads the session. */

import type {HypothesisEntry} from '@vibesys/backend-client';
import {hasRunEnded} from '@vibesys/core-state';
import {type Dispatch, useEffect, useMemo, useReducer, useState, useSyncExternalStore} from 'react';
import {latestRound, needsOlder, runControl, steersNeedOlder} from './derive.js';
import {type HomeApi, type HomeProject, type HomeRun, openRun, sidebarSections} from './home.js';
import {
  type RetainedText,
  type RoundRow,
  type RunSummary,
  type RunTitle,
  resultParts,
  retainedText,
  runSummary,
  runTitle,
  type StatusLine,
  statusLine,
} from './rounds.js';
import type {WorkspaceSession, WorkspaceState} from './session.js';
import {type RoundTranscript, roundTranscript, toolDetail} from './transcript.js';
import {Banner} from './ui/Banner.js';
import {Sidebar} from './ui/Sidebar.js';
import {SteerComposer} from './ui/SteerComposer.js';
import {MoreMenu, Retained, RunControlChip, RunStatus, TitleRow} from './ui/TitleRow.js';
import {Transcript, type TranscriptControls} from './ui/Transcript.js';
import {forRun, INITIAL_UI, type UiAction, type UiState, uiReducer} from './ui-state.js';
import './window.css';

const NO_EXPERIMENTS: HypothesisEntry[] = [];
const ACTION_WORDS = {pause: 'Pause', resume: 'Resume', steer: 'Steer', stop: 'Stop'} as const;

export interface AppProps {
  session: WorkspaceSession;
  home: HomeApi;
}

interface View {
  summary: RunSummary;
  live: number | null;
  round: number | null;
  row: RoundRow | undefined;
  transcript: RoundTranscript | null;
  title: RunTitle;
  status: StatusLine;
  retained: RetainedText | null;
  ended: boolean;
}

interface History {
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}

interface Listing {
  projects: HomeProject[];
  runs: HomeRun[];
}

const EMPTY_LISTING: Listing = {projects: [], runs: []};

function useRunView(state: WorkspaceState, ui: UiState): View {
  const {core, captured, runId, queries, sent} = state;
  const experiments = queries.experiments.response?.experiments ?? NO_EXPERIMENTS;
  const context = queries.performance.response?.performance_context ?? null;
  const summary = useMemo(
    () => runSummary(core, captured, experiments, context),
    [core, captured, experiments, context],
  );
  const live = latestRound(core);
  const round = ui.round ?? live;
  const transcript = useMemo(
    () => (round === null ? null : roundTranscript({core, captured, sent, round, runId})),
    [core, captured, sent, round, runId],
  );
  return {
    summary,
    live,
    round,
    row: summary.rows.find(row => row.round === round),
    transcript,
    title: runTitle(context, captured, runId),
    status: statusLine(core, state.command.sending),
    retained: retainedText(summary),
    ended: hasRunEnded(core),
  };
}

function useHome(home: HomeApi): Listing {
  const [listing, setListing] = useState(EMPTY_LISTING);
  useEffect(() => {
    let current = true;
    home
      .projects()
      .then(async projects => {
        const runs = (await Promise.all(projects.map(project => home.runs(project.id)))).flat();
        if (current) setListing({projects, runs});
      })
      // ponytail: a failed listing leaves the open run only; sub-project 4 adds its error state.
      .catch(() => {});
    return () => {
      current = false;
    };
  }, [home]);
  return listing;
}

/** Backfills older events while the selected round (or a steer it consumed) sits below the floor. */
function useBackfill(
  session: WorkspaceSession,
  state: WorkspaceState,
  round: number | null,
): History {
  const {core, captured, historyLoading, historyError} = state;
  const wants =
    round !== null && (needsOlder(core, round) || steersNeedOlder(core, captured, round));
  useEffect(() => {
    if (wants && !historyLoading && historyError === null) void session.loadOlder();
  }, [session, wants, historyLoading, historyError]);
  return {
    loading: wants && historyLoading,
    error: wants ? historyError : null,
    onRetry: () => void session.loadOlder(),
  };
}

/** The UI state of the session's current run; a replaced run starts from a clean selection. */
function useRunUi(runId: string | null): [UiState, Dispatch<UiAction>] {
  const [stored, dispatch] = useReducer(uiReducer, INITIAL_UI);
  useEffect(() => {
    if (stored.runId !== runId) dispatch({type: 'run', runId});
  }, [stored.runId, runId]);
  return [forRun(stored, runId), dispatch];
}

function transcriptControls(
  state: WorkspaceState,
  ui: UiState,
  dispatch: Dispatch<UiAction>,
): TranscriptControls {
  return {
    expanded: ui.expanded,
    disclosed: ui.disclosed,
    detail: id => toolDetail(state.core, id),
    onExpand: id => dispatch({type: 'expand', id}),
    onDisclose: key => dispatch({type: 'disclose', key}),
  };
}

function toggleRun(session: WorkspaceSession, state: WorkspaceState): void {
  const control = runControl(state.core, state.captured, state.connection);
  if (control.kind !== 'action' || control.disabled) return;
  void session.command(
    control.action === 'pause'
      ? {type: 'command.pause', mode: 'after_current_agent_call'}
      : {type: 'command.resume'},
  );
}

function canStop(state: WorkspaceState, view: View): boolean {
  return !view.ended && state.core.status !== 'stopping' && state.connection === 'connected';
}

interface SectionProps {
  state: WorkspaceState;
  view: View;
  ui: UiState;
  dispatch: Dispatch<UiAction>;
  session: WorkspaceSession;
}

function RunSidebar({state, view, ui, dispatch, listing}: SectionProps & {listing: Listing}) {
  const open = openRun({
    runId: state.runId,
    title: view.title.title,
    project: view.title.project,
    status: state.core.status,
    updatedAt: state.core.lastEventTimestamp,
  });
  return (
    <Sidebar
      width={ui.sideWidth}
      sections={sidebarSections(listing.projects, listing.runs, open)}
      current={state.runId}
      summary={view.summary}
      selected={view.round}
      now={new Date()}
      onRound={round => dispatch({type: 'round', round, live: view.live})}
    />
  );
}

function RunHeader({state, view, ui, dispatch, session}: SectionProps) {
  const error = state.command.error;
  const stop = () => {
    dispatch({type: 'menu', menu: null});
    void session.command({type: 'command.stop', mode: 'after_current_agent_call'});
  };
  return (
    <TitleRow
      title={view.title.title}
      objective={view.title.objective}
      project={view.title.project}
    >
      <RunStatus line={view.status} />
      {error !== null && error.action !== 'steer' ? (
        <span
          className="cmderr bad"
          role="alert"
        >{`${ACTION_WORDS[error.action]} failed: ${error.message}`}</span>
      ) : null}
      <RunControlChip
        control={runControl(state.core, state.captured, state.connection)}
        busy={view.status.busy}
        onToggle={() => toggleRun(session, state)}
      />
      {view.retained === null ? null : (
        <>
          <span className="vsep" />
          <Retained text={view.retained} />
        </>
      )}
      <span className="vsep" />
      <MoreMenu
        menu={ui.menu}
        canStop={canStop(state, view)}
        stopWho={view.status.activeKind === 'judge' ? 'judge' : 'current agent'}
        runId={state.runId}
        onMenu={menu => dispatch({type: 'menu', menu})}
        onStop={stop}
      />
    </TitleRow>
  );
}

function RunTranscript({state, view, ui, dispatch, history}: SectionProps & {history: History}) {
  const connecting = state.core.status === 'connecting' && state.core.sequence === 0;
  const {row, summary} = view;
  return (
    <Transcript
      round={view.round}
      row={row ?? null}
      result={row === undefined ? [] : resultParts(row, summary.unit, summary.lowerIsBetter)}
      model={view.transcript}
      follow={!view.ended && view.round === view.live}
      history={history}
      empty={connecting ? 'Connecting…' : 'Waiting for round 1.'}
      controls={transcriptControls(state, ui, dispatch)}
      only={ui.agent}
      onShowAll={() => dispatch({type: 'agent', id: null})}
    />
  );
}

function RunComposer({state, view, session}: SectionProps) {
  if (view.ended) return null;
  const error = state.command.error;
  const connected = state.connection === 'connected';
  return (
    <SteerComposer
      disabled={!connected}
      reason={connected ? null : 'Steering resumes when the connection returns'}
      error={error?.action === 'steer' ? `Steer failed: ${error.message}` : null}
      onSend={text => session.command({type: 'command.steer', text})}
    />
  );
}

export function App({session, home}: AppProps) {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const [ui, dispatch] = useRunUi(state.runId);
  const view = useRunView(state, ui);
  const listing = useHome(home);
  const history = useBackfill(session, state, view.round);
  const section: SectionProps = {state, view, ui, dispatch, session};
  return (
    <div className="win">
      <RunSidebar {...section} listing={listing} />
      <main className="main">
        <RunHeader {...section} />
        <Banner
          connection={state.connection}
          connectionError={state.connectionError}
          canRetry={state.canRetry}
          snapshotError={state.snapshotError}
          onReconnect={() => void session.reconnect()}
          onRetrySnapshot={() => void session.refresh()}
        />
        <RunTranscript {...section} history={history} />
        <RunComposer {...section} />
      </main>
    </div>
  );
}
