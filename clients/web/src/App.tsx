/** The run window over one WorkspaceSession: the only component that reads the session. */

import type {HypothesisEntry} from '@vibesys/backend-client';
import {hasRunEnded} from '@vibesys/core-state';
import {
  type Dispatch,
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useState,
  useSyncExternalStore,
} from 'react';
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
import {AgentsTab} from './ui/Agents.js';
import {Banner} from './ui/Banner.js';
import {ChangesTab} from './ui/Changes.js';
import {Pane, Placeholder} from './ui/Pane.js';
import {Resizer} from './ui/Resizer.js';
import {Sidebar} from './ui/Sidebar.js';
import {SteerComposer} from './ui/SteerComposer.js';
import {
  MoreMenu,
  PaneToggle,
  Retained,
  RunControlChip,
  RunStatus,
  SidebarToggle,
  TitleRow,
} from './ui/TitleRow.js';
import {Transcript, type TranscriptControls} from './ui/Transcript.js';
import {
  forRun,
  frame,
  INITIAL_UI,
  type PaneTab,
  SIDE,
  type UiAction,
  type UiState,
  uiReducer,
} from './ui-state.js';
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

function subscribeWidth(notify: () => void): () => void {
  addEventListener('resize', notify);
  return () => removeEventListener('resize', notify);
}

function useWindowWidth(): number {
  return useSyncExternalStore(subscribeWidth, () => innerWidth);
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
      head={<SidebarToggle shown onToggle={() => dispatch({type: 'sidebar', open: false})} />}
      resizer={
        <Resizer
          label="Resize the sidebar"
          edge="right"
          value={ui.sideWidth}
          min={SIDE.min}
          max={SIDE.max}
          grow={1}
          widthAt={clientX => clientX}
          onChange={width => dispatch({type: 'resize', target: 'side', width})}
        />
      }
    />
  );
}

function RunHeader(props: SectionProps & {sidebarShown: boolean; onShowSidebar: () => void}) {
  const {state, view, ui, dispatch, session} = props;
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
      leading={
        props.sidebarShown ? undefined : (
          <SidebarToggle shown={false} onToggle={props.onShowSidebar} />
        )
      }
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
      <PaneToggle open={ui.pane !== null} onToggle={() => dispatch({type: 'togglePane'})} />
      <MoreMenu
        menu={ui.menu}
        canStop={canStop(state, view)}
        stopWho={view.status.activeKind === 'judge' ? 'judge' : 'current agent'}
        runId={state.runId}
        onMenu={menu => dispatch({type: 'menu', menu})}
        onStop={stop}
      >
        <button
          type="button"
          role="menuitem"
          className="it"
          onClick={() => dispatch({type: 'pane', pane: 'notes'})}
        >
          Notes
        </button>
      </MoreMenu>
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

interface PaneHostProps extends SectionProps {
  tab: PaneTab;
  width: number;
  controls: TranscriptControls;
  onAgent: (id: string) => void;
}

function changesTab(props: PaneHostProps) {
  const design = props.state.queries.design;
  return (
    <ChangesTab
      row={props.view.row}
      design={design.response?.design?.find(entry => entry.round === props.view.round)}
      loading={design.response === null && design.error === null}
      error={design.error}
      onRetry={() => void props.session.load('design')}
      loadPatch={props.session.designPatch}
    />
  );
}

function agentsTab(props: PaneHostProps) {
  const {view} = props;
  if (view.round === null) return <Placeholder scope="Run" text="No round has started yet." />;
  return (
    <AgentsTab
      core={props.state.core}
      round={view.round}
      turns={view.transcript?.turns ?? []}
      selected={props.ui.agent}
      width={props.width}
      controls={props.controls}
      onSelect={props.onAgent}
    />
  );
}

function PaneBody(props: PaneHostProps) {
  const scope = props.view.round === null ? 'Run' : `Round ${props.view.round}`;
  switch (props.tab) {
    case 'ask':
      return <Placeholder scope="Run" text="Chat about this run is not available yet." />;
    case 'notes':
      return <Placeholder scope="Run" text="Run notes are not available yet." />;
    case 'changes':
      return changesTab(props);
    case 'agents':
      return agentsTab(props);
    case 'experiments':
      return <Placeholder scope={scope} text="Not available yet." />;
  }
}

function RunPane(props: PaneHostProps) {
  const {dispatch} = props;
  return (
    <Pane
      tab={props.tab}
      width={props.width}
      onTab={pane => dispatch({type: 'pane', pane})}
      onClose={() => dispatch({type: 'pane', pane: null})}
      onResize={width => dispatch({type: 'resize', target: 'pane', width})}
    >
      <PaneBody {...props} />
    </Pane>
  );
}

export function App({session, home}: AppProps) {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const [ui, dispatch] = useRunUi(state.runId);
  const view = useRunView(state, ui);
  const listing = useHome(home);
  const history = useBackfill(session, state, view.round);
  const width = useWindowWidth();
  const layout = frame(width, ui);
  const selectAgent = useCallback((id: string) => dispatch({type: 'agent', id}), [dispatch]);
  const section: SectionProps = {state, view, ui, dispatch, session};
  const toggleSidebar = () => {
    if (layout.sidebar) return dispatch({type: 'sidebar', open: false});
    dispatch({type: 'sidebar', open: true});
    // Narrow with a pane open, the sidebar can only return by taking the pane's room.
    if (!frame(width, {...ui, sidebar: true}).sidebar) dispatch({type: 'pane', pane: null});
  };
  const controls = transcriptControls(state, ui, dispatch);
  return (
    <div className="win">
      {layout.sidebar ? <RunSidebar {...section} listing={listing} /> : null}
      <main className="main">
        <RunHeader {...section} sidebarShown={layout.sidebar} onShowSidebar={toggleSidebar} />
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
      {ui.pane === null ? null : (
        <RunPane
          {...section}
          tab={ui.pane}
          width={layout.paneWidth}
          controls={controls}
          onAgent={selectAgent}
        />
      )}
    </div>
  );
}
