/** The run window over one WorkspaceSession: the only component that reads the session. */

import type {DesignRound, HypothesisEntry} from '@vibesys/backend-client';
import {hasRunEnded} from '@vibesys/core-state';
import {
  type Dispatch,
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import {type AskView, askView, chatOffer} from './ask.js';
import {type ControlsBanner, controlsBanner} from './banners.js';
import {copyText} from './clipboard.js';
import {activityRound, attachNote, needsOlder, runControl, steersNeedOlder} from './derive.js';
import {type HomeApi, type Listing, openRun, sidebarSections} from './home.js';
import {useHome, useNewRunShortcut, usePaletteShortcut} from './home-hooks.js';
import {asDraft, type NoteController, type NotesApi, useNote} from './notes.js';
import {type Intent, type PaletteInput, paletteItems} from './palette.js';
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
import type {RunLinks} from './route.js';
import type {WorkspaceSession, WorkspaceState} from './session.js';
import {type ThemeChoice, useTheme} from './theme.js';
import {
  type LineStat,
  type RoundEdits,
  type RoundTranscript,
  roundEdits,
  roundTranscript,
  toolDetail,
} from './transcript.js';
import {AgentsTab} from './ui/Agents.js';
import {AskTab} from './ui/Ask.js';
import {Banner} from './ui/Banner.js';
import {ChangesTab} from './ui/Changes.js';
import {ExperimentsTab} from './ui/Experiments.js';
import {NotesTab} from './ui/Notes.js';
import {Palette} from './ui/Palette.js';
import {Pane, Placeholder} from './ui/Pane.js';
import {Resizer} from './ui/Resizer.js';
import {NewRunRow, SearchRow, Sidebar} from './ui/Sidebar.js';
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
  agentFilter,
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
const NO_DESIGN: DesignRound[] = [];
const NO_EDITS: ReadonlyMap<string, LineStat> = new Map();
const ACTION_WORDS = {pause: 'Pause', resume: 'Resume', steer: 'Steer', stop: 'Stop'} as const;

export interface AppProps {
  session: WorkspaceSession;
  home: HomeApi;
  links: RunLinks | null;
  /** The home server's notes API; null when the page was opened without the home token. */
  notes: NotesApi | null;
  /** The theme the page opened with (see main.tsx). */
  theme: ThemeChoice;
}

interface View {
  summary: RunSummary;
  live: number | null;
  round: number | null;
  row: RoundRow | undefined;
  transcript: RoundTranscript | null;
  edits: RoundEdits;
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

function useRunView(state: WorkspaceState, ui: UiState): View {
  const {core, captured, runId, queries, sent} = state;
  const experiments = queries.experiments.response?.experiments ?? NO_EXPERIMENTS;
  const context = queries.performance.response?.performance_context ?? null;
  const summary = useMemo(
    () => runSummary(core, captured, experiments, context),
    [core, captured, experiments, context],
  );
  const live = activityRound(core);
  const round = ui.round === 'live' ? live : ui.round;
  const transcript = useMemo(
    () => roundTranscript({core, captured, sent, round, runId}),
    [core, captured, sent, round, runId],
  );
  const edits = useMemo(() => roundEdits(core, runId), [core, runId]);
  return {
    summary,
    live,
    round,
    row: summary.rows.find(row => row.round === round),
    transcript,
    edits,
    title: runTitle(context, captured, runId),
    status: statusLine(core, state.command.sending),
    retained: retainedText(summary),
    ended: hasRunEnded(core),
  };
}

/** Backfills older events while the selected round (or a steer it consumed) sits below the floor. */
function useBackfill(
  session: WorkspaceSession,
  state: WorkspaceState,
  round: number | null,
): History {
  const {core, captured, historyLoading, historyError} = state;
  const wants = needsOlder(core, round) || steersNeedOlder(core, captured, round);
  useEffect(() => {
    if (wants && !historyLoading && historyError === null) void session.loadOlder();
  }, [session, wants, historyLoading, historyError]);
  return {
    loading: wants && historyLoading,
    error: wants ? historyError : null,
    onRetry: () => void session.loadOlder(),
  };
}

interface AskHost {
  view: AskView;
  error: string | null;
  start: (selection: {provider: string; model: string} | null) => void;
}

function useAsk(
  session: WorkspaceSession,
  state: WorkspaceState,
  ui: UiState,
  dispatch: Dispatch<UiAction>,
): AskHost {
  const {core, captured, asks} = state;
  const {options, checking, failed} = chatOffer(state.queries.chat_options);
  const view = useMemo(
    () =>
      askView({
        threads: core.chatThreads,
        transcripts: core.chatTranscripts,
        captured,
        asks,
        options,
        checking,
        failed,
        selected: ui.thread,
        picked: ui.threadModel,
      }),
    [
      core.chatThreads,
      core.chatTranscripts,
      captured,
      asks,
      options,
      checking,
      failed,
      ui.thread,
      ui.threadModel,
    ],
  );
  // Options are asked for where they show (Ask, Notes, the palette), again as the run's status
  // moves (a run still starting reports none yet), and when the connection returns (which also
  // retries a query that failed).
  const wanted = ui.pane === 'ask' || ui.pane === 'notes' || ui.palette;
  const offered = view.harness === 'available';
  const connected = state.connection === 'connected';
  useEffect(() => {
    if (wanted && !offered && connected && core.status !== 'connecting')
      void session.load('chat_options');
  }, [session, wanted, offered, connected, core.status]);
  // A question that failed goes back into an empty composer, to resend without retyping.
  const settled = useRef(new Set<string>());
  const draft = ui.drafts.ask;
  useEffect(() => {
    for (const ask of asks) {
      if (ask.error === null || settled.current.has(ask.id)) continue;
      settled.current.add(ask.id);
      if (draft === '') dispatch({type: 'draft', target: 'ask', text: ask.text});
    }
  }, [asks, draft, dispatch]);
  const [failure, setFailure] = useState<{runId: string | null; message: string} | null>(null);
  // A thread error belongs to the run it happened in.
  const error = failure !== null && failure.runId === state.runId ? failure.message : null;
  const start = (selection: {provider: string; model: string} | null) => {
    const runId = state.runId;
    setFailure(null);
    dispatch({type: 'menu', menu: null});
    session.createThread(selection).then(
      id => dispatch({type: 'thread', id, model: selection}),
      (reason: unknown) =>
        setFailure({
          runId,
          message: `Could not start a thread: ${reason instanceof Error ? reason.message : String(reason)}`,
        }),
    );
  };
  return {view, error, start};
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
  return (
    !view.ended &&
    state.core.status !== 'stopping' &&
    state.connection === 'connected' &&
    state.command.sending === null
  );
}

interface SectionProps {
  state: WorkspaceState;
  view: View;
  ui: UiState;
  dispatch: Dispatch<UiAction>;
  session: WorkspaceSession;
  links: RunLinks | null;
}

function RunSidebar({
  state,
  view,
  ui,
  dispatch,
  links,
  listing,
}: SectionProps & {listing: Listing}) {
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
      note={attachNote(state.queries.experiments.response, view.ended)}
      selected={view.round}
      activity={state.core.phases.some(phase => phase.roundNumber === null)}
      now={new Date()}
      onRound={round => dispatch({type: 'round', round, live: view.live})}
      head={<SidebarToggle shown onToggle={() => dispatch({type: 'sidebar', open: false})} />}
      nav={
        <>
          {links === null ? null : <NewRunRow href={links.newRun} on={false} />}
          <SearchRow onOpen={() => dispatch({type: 'palette', open: true})} />
        </>
      }
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

function RunHeader(
  props: SectionProps & {
    sidebarShown: boolean;
    onShowSidebar: () => void;
    theme: ThemeChoice;
    onTheme: (choice: ThemeChoice) => void;
  },
) {
  const {state, view, ui, dispatch, session} = props;
  const error = state.command.error;
  const control = runControl(state.core, state.captured, state.connection);
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
      <RunStatus line={view.status} control={control} />
      {error !== null && error.action !== 'steer' ? (
        <span
          className="cmderr bad"
          role="alert"
        >{`${ACTION_WORDS[error.action]} failed: ${error.message}`}</span>
      ) : null}
      <RunControlChip
        control={control}
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
        resume={
          props.links !== null && view.ended && state.runId !== null
            ? props.links.resume(state.runId)
            : null
        }
        theme={props.theme}
        onMenu={menu => dispatch({type: 'menu', menu})}
        onStop={stop}
        onNotes={() => dispatch({type: 'pane', pane: 'notes'})}
        onTheme={props.onTheme}
      />
    </TitleRow>
  );
}

/** A paused run whose latest round finished says what resuming starts. */
function resumeLine(state: WorkspaceState, view: View): string | null {
  const {row, round, live} = view;
  if (state.core.status !== 'paused' || round === null || round !== live) return null;
  if (row === undefined || row.state === 'running') return null;
  // The budget is spent: no round follows.
  if (view.summary.planned === 0) return null;
  return `Round ${round + 1} starts when you resume.`;
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
      endline={resumeLine(state, view)}
      controls={transcriptControls(state, ui, dispatch)}
      only={agentFilter(ui, view.round)}
      onShowAll={() => dispatch({type: 'agent', id: null, round: view.round})}
    />
  );
}

function RunComposer({state, view, ui, dispatch, session}: SectionProps) {
  if (view.ended) return null;
  const error = state.command.error;
  const connected = state.connection === 'connected';
  return (
    <SteerComposer
      disabled={!connected}
      reason={connected ? null : 'Steering resumes when the connection returns'}
      error={error?.action === 'steer' ? `Steer failed: ${error.message}` : null}
      draft={ui.drafts.steer}
      onDraft={text => dispatch({type: 'draft', target: 'steer', text})}
      onSend={text => session.command({type: 'command.steer', text})}
    />
  );
}

interface PaneHostProps extends SectionProps {
  tab: PaneTab;
  width: number;
  controls: TranscriptControls;
  onAgent: (id: string) => void;
  ask: AskHost;
  note: NoteController;
}

function changesTab(props: PaneHostProps) {
  if (props.view.round === null) {
    return (
      <Placeholder
        scope="Run"
        text={
          props.view.summary.rows.length === 0
            ? 'No round results yet.'
            : 'Select a round to inspect changes.'
        }
      />
    );
  }
  const design = props.state.queries.design;
  return (
    <ChangesTab
      row={props.view.row}
      design={design.response?.design?.find(entry => entry.round === props.view.round)}
      loading={design.response === null && design.error === null}
      error={design.error}
      onRetry={() => void props.session.load('design')}
      loadPatch={props.session.designPatch}
      edits={
        (props.view.round === null ? undefined : props.view.edits.get(props.view.round)) ?? NO_EDITS
      }
    />
  );
}

function agentsTab(props: PaneHostProps) {
  const {view} = props;
  return (
    <AgentsTab
      core={props.state.core}
      round={view.round}
      turns={view.transcript?.turns ?? []}
      selected={agentFilter(props.ui, view.round)}
      width={props.width}
      controls={props.controls}
      onSelect={props.onAgent}
    />
  );
}

function experimentsTab(props: PaneHostProps) {
  const {dispatch, view, state} = props;
  const open = (round: number) => dispatch({type: 'round', round, live: view.live});
  return (
    <ExperimentsTab
      summary={view.summary}
      experiments={state.queries.experiments.response?.experiments ?? NO_EXPERIMENTS}
      designRounds={state.queries.design.response?.design ?? NO_DESIGN}
      captured={state.captured}
      edits={view.edits}
      maxRounds={state.core.maxRounds}
      view={props.ui.experimentsView}
      open={props.ui.evidence}
      onView={next => dispatch({type: 'experimentsView', view: next})}
      onToggle={round => dispatch({type: 'evidence', round})}
      onRound={open}
      onChanges={round => {
        open(round);
        dispatch({type: 'pane', pane: 'changes'});
      }}
    />
  );
}

function askTab(props: PaneHostProps) {
  const {state, ui, dispatch, session, ask} = props;
  const connected = state.connection === 'connected';
  return (
    <AskTab
      view={ask.view}
      menu={ui.menu}
      draft={ui.drafts.ask}
      reason={connected ? null : 'Asking resumes when the connection returns'}
      error={ask.error}
      onMenu={menu => dispatch({type: 'menu', menu})}
      onThread={id => dispatch({type: 'thread', id})}
      onNewThread={ask.start}
      onDraft={text => dispatch({type: 'draft', target: 'ask', text})}
      onSend={text => Promise.resolve(session.ask(text, ask.view.current.id))}
      onRetry={() => void session.load('chat_options')}
    />
  );
}

function notesTab(props: PaneHostProps) {
  const {view, dispatch, note, ask} = props;
  const draft = note.state.phase === 'ready' ? asDraft(note.state.text) : '';
  const focus = (id: string) => requestAnimationFrame(() => document.getElementById(id)?.focus());
  return (
    <NotesTab
      note={note.state}
      canSteer={!view.ended}
      harness={ask.view.harness}
      onEdit={note.edit}
      onBlur={note.flush}
      onRetry={note.retry}
      onSteerDraft={() => {
        dispatch({type: 'draft', target: 'steer', text: draft});
        focus('steer');
      }}
      onAskDraft={() => {
        dispatch({type: 'draft', target: 'ask', text: draft});
        dispatch({type: 'pane', pane: 'ask'});
        focus('ask');
      }}
    />
  );
}

function PaneBody(props: PaneHostProps) {
  switch (props.tab) {
    case 'ask':
      return askTab(props);
    case 'notes':
      return notesTab(props);
    case 'changes':
      return changesTab(props);
    case 'agents':
      return agentsTab(props);
    case 'experiments':
      return experimentsTab(props);
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

interface IntentContext {
  dispatch: Dispatch<UiAction>;
  session: WorkspaceSession;
  state: WorkspaceState;
  toggleSidebar: () => void;
  links: RunLinks | null;
  newThread: () => void;
  chooseTheme: (choice: ThemeChoice) => void;
}

function runIntent(intent: Intent, context: IntentContext): void {
  const {dispatch} = context;
  dispatch({type: 'palette', open: false});
  switch (intent.kind) {
    case 'ui':
      dispatch(intent.action);
      return;
    case 'toggleRun':
      toggleRun(context.session, context.state);
      return;
    case 'copyRunId':
      // No message slot here (the palette has already closed): a failure is silent past this log.
      if (context.state.runId !== null)
        void copyText(context.state.runId).then(ok => {
          if (!ok) console.error("Couldn't copy the run ID to the clipboard.");
        });
      return;
    case 'sidebar':
      context.toggleSidebar();
      return;
    case 'steer':
      requestAnimationFrame(() => document.getElementById('steer')?.focus());
      return;
    case 'reveal':
      dispatch({type: 'disclose', key: intent.key, open: true});
      requestAnimationFrame(() =>
        document
          .querySelector(`[data-turn="${CSS.escape(intent.turn)}"]`)
          ?.scrollIntoView({block: 'start'}),
      );
      return;
    case 'newRun':
      if (context.links !== null) window.location.assign(context.links.newRun);
      return;
    case 'open':
      window.location.assign(intent.href);
      return;
    case 'newThread':
      dispatch({type: 'pane', pane: 'ask'});
      context.newThread();
      return;
    case 'askMenu':
      dispatch({type: 'pane', pane: 'ask'});
      dispatch({type: 'menu', menu: intent.menu});
      return;
    case 'theme':
      context.chooseTheme(intent.theme);
      return;
  }
}

/** Palette items from what the window shows now: the agent filter's turns only, so none is a no-op. */
function paletteInput(
  state: WorkspaceState,
  view: View,
  ui: UiState,
  sidebarShown: boolean,
  links: RunLinks | null,
  extra: {ask: AskView; theme: ThemeChoice},
): PaletteInput {
  const {ask, theme} = extra;
  const only = agentFilter(ui, view.round);
  const visible = (view.transcript?.turns ?? []).filter(turn => only === null || turn.id === only);
  const withPrompt = visible.filter(turn => turn.prompt !== null).at(-1);
  const withTodos = visible.filter(turn => turn.todos.length > 0).at(-1);
  const where = (phase: string) =>
    [`round ${view.round ?? ''}`, phase.toLowerCase()].filter(Boolean).join(', ');
  return {
    control: runControl(state.core, state.captured, state.connection),
    pending: view.status.busy,
    canStop: canStop(state, view),
    canSteer: !view.ended && state.connection === 'connected' && state.command.sending === null,
    hasRunId: state.runId !== null,
    rows: view.summary.rows,
    live: view.live,
    selected: view.round,
    pane: ui.pane,
    sidebarShown,
    newRun: links !== null,
    resumeHref:
      links !== null && view.ended && state.runId !== null ? links.resume(state.runId) : null,
    prompt:
      withPrompt === undefined ? null : {turn: withPrompt.id, detail: where(withPrompt.phase)},
    todos: withTodos === undefined ? null : {turn: withTodos.id, detail: where(withTodos.phase)},
    ask:
      ask.harness === 'available' ? {threads: ask.threads.length, model: ask.current.model} : null,
    theme,
  };
}

export function App({session, home, links, notes, theme: opened}: AppProps) {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const [ui, dispatch] = useRunUi(state.runId);
  const view = useRunView(state, ui);
  const listing = useHome(home);
  const history = useBackfill(session, state, view.round);
  const ask = useAsk(session, state, ui, dispatch);
  const note = useNote(notes, state.runId, ui.pane === 'notes');
  const [theme, chooseTheme] = useTheme(opened);
  usePaletteShortcut(useCallback(() => dispatch({type: 'palette', open: true}), [dispatch]));
  useNewRunShortcut(links?.newRun ?? null);
  const width = useWindowWidth();
  const layout = frame(width, ui);
  const selectAgent = useCallback(
    (id: string) => dispatch({type: 'agent', id, round: view.round}),
    [dispatch, view.round],
  );
  const section: SectionProps = {state, view, ui, dispatch, session, links};
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
        <RunHeader
          {...section}
          sidebarShown={layout.sidebar}
          onShowSidebar={toggleSidebar}
          theme={theme}
          onTheme={chooseTheme}
        />
        <Banner
          connection={state.connection}
          connectionError={state.connectionError}
          canRetry={state.canRetry}
          snapshotError={state.snapshotError}
          onReconnect={() => void session.reconnect()}
          onRetrySnapshot={() => void session.refresh()}
        />
        <ControlsOutage
          banner={controlsBanner(state)}
          onReconnect={() => session.reconnectControls()}
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
          ask={ask}
          note={note}
        />
      )}
      {ui.palette ? (
        <Palette
          items={paletteItems(
            paletteInput(state, view, ui, layout.sidebar, links, {ask: ask.view, theme}),
          )}
          placeholder="Search commands, rounds and views…"
          onRun={entry =>
            runIntent(entry.intent, {
              dispatch,
              session,
              state,
              toggleSidebar,
              links,
              newThread: () => ask.start(null),
              chooseTheme,
            })
          }
          onClose={() => dispatch({type: 'palette', open: false})}
        />
      ) : null}
    </div>
  );
}

/**
 * The command path is undeliverable: every query and every control (pause,
 * resume, steer, stop, chat) is held until the channel comes back, while the
 * transcript above keeps streaming. A banner of its own and not the connection
 * one, because the two sockets fail independently and the recovery differs.
 */
function ControlsOutage({
  banner,
  onReconnect,
}: {
  banner: ControlsBanner | null;
  onReconnect: () => void;
}) {
  if (banner === null) return null;
  return (
    <div
      className="banner"
      role="alert"
      aria-label="Run controls status"
      data-testid="controls-banner"
    >
      <span>{banner.message}</span>
      {/*
        Disabled rather than hidden while a dial is in flight: `reconnect()`
        no-ops then, so an enabled button would swallow clicks for the whole
        connect-timeout window and change nothing on screen. Hiding it instead
        would make the affordance flicker in and out.
      */}
      <button type="button" className="btn" disabled={banner.retrying} onClick={onReconnect}>
        {banner.retrying ? 'Reconnecting…' : 'Reconnect now'}
      </button>
    </div>
  );
}
