import {type CoreState, EMPTY_LEDGER} from '@vibesys/run-state';
import {
  Banner,
  Breadcrumb,
  Button,
  Card,
  Chip,
  type Crumb,
  Dock,
  Fact,
  FactLine,
  LogPane,
  Menu,
  type MenuItem,
  markdownToPlain,
  Reserve,
  Row,
  Screen,
  Stack,
  Text,
  Timeline,
} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {
  clockAt,
  clockStart,
  endedView,
  type HistoryProgress,
  headerView,
  historyLoading,
  historyProblem,
  ideaAncestry,
  type ModelNames,
  machineBlockWords,
  NO_MODEL_NAMES,
  type Offer,
  pauseNowBannerView,
  predatesTraceWords,
  type ResolvedZoom,
  type Route,
  RUN_ROUTE,
  type RunModel,
  type RunPageAction,
  type RunPageState,
  resolveZoom,
  roleLabel,
  roundsBeforeTrace,
  routeLevel,
  runGoneWords,
  runHasEnded,
  runNow,
  runPhase,
  runWallNow,
  stepLabel,
  streamLines,
  timelineView,
  traceArrival,
  transcriptLoading,
  unplacedRun,
  usageRecords,
  whoBySpan,
} from '../../state/index.js';
import {Hero} from './hero.js';
import {
  HistoryLoading,
  InitialRunLoading,
  RunGone,
  RunLoadError,
  RunUnplaced,
} from './history-loading.js';
import {DeepLevel, IdeasWorkspace, type LevelProps} from './levels.js';
import {RunLevel} from './run-level.js';
import {RunScrubber} from './run-scrubber.js';
import type {ViewBase, Zoom} from './views/props.js';

type Dispatch = (action: RunPageAction) => void;

function LevelBody(props: {
  readonly deep: boolean;
  readonly route: Route;
  readonly zoomed: ResolvedZoom;
  readonly level: LevelProps;
}): JSX.Element | null {
  if (
    props.zoomed.step === null &&
    props.zoomed.agent === null &&
    (props.route.mode === 'ideas' || props.zoomed.idea !== null)
  ) {
    return <IdeasWorkspace {...props.level} idea={props.zoomed.idea} />;
  }
  return props.deep ? (
    <DeepLevel {...props.level} zoomed={props.zoomed} />
  ) : (
    <RunLevel {...props.level} />
  );
}

/**
 * What the breadcrumb row is as tall as: a step to go back to and the place you are at. The row is
 * drawn empty and kept where no breadcrumb shows, so the panes below it stay where they are when
 * a step opens and the breadcrumb appears.
 */
const BREADCRUMB_ROW: readonly Crumb[] = [
  {id: 'back', label: 'Run', press: () => undefined},
  {id: 'here', label: 'Here'},
];

function showsBreadcrumb(route: Route, zoomed: ResolvedZoom): boolean {
  if (routeLevel(route) === 'run') return false;
  return !(zoomed.idea !== null && zoomed.step === null && zoomed.agent === null);
}

function reason(offer: Offer): {disabledReason?: string} {
  return offer.disabledReason === null ? {} : {disabledReason: offer.disabledReason};
}

/** The chat panel the app hosts beside the run: whether it is open, the button's press, the panel. */
export interface RunChatSlot {
  readonly minimumMain?: 'wide';
  readonly open: boolean;
  readonly toggle: () => void;
  readonly panel: ReactNode;
  /** Why Chat cannot open (the machine's VibeSys cannot serve an ended run); absent: it can. */
  readonly unavailable?: string;
}

/**
 * The run's facts under its title: its status and id on one line, the id whole (the line wraps
 * rather than cut it); the agents and the spend on the next, so the spend takes no line of its own.
 */
function RunFacts(props: {readonly header: ReturnType<typeof headerView>}): JSX.Element {
  const {header} = props;
  return (
    <>
      <FactLine fit="parent" wrap>
        <Fact>
          <Chip label={header.status.label} tone={header.status.tone} dot />
        </Fact>
        <Fact title={header.subtitle}>
          <Text variant="code" tone="muted">
            {header.subtitle}
          </Text>
        </Fact>
      </FactLine>
      {header.agents.length === 0 && header.spent === null ? null : (
        <FactLine>
          {header.agents.map(agent => (
            <Fact key={agent.role}>
              <Text variant="small" tone="muted">
                {agent.role}
              </Text>
              <Text variant="code">{agent.ran.join(', ')}</Text>
            </Fact>
          ))}
          {header.spent === null ? null : (
            <Fact>
              <Text variant="code" tone="muted" title={header.spent.title}>
                {header.agents.length === 0 ? header.spent.label : `· ${header.spent.label}`}
              </Text>
            </Fact>
          )}
        </FactLine>
      )}
    </>
  );
}

function Header(props: {
  readonly state: RunPageState;
  readonly names: ModelNames;
  readonly dispatch: Dispatch;
  readonly chat: RunChatSlot | undefined;
  readonly toolbar?: ReactNode;
}): JSX.Element {
  const {state, dispatch, chat} = props;
  const header = headerView(state, props.names);
  const items: MenuItem[] = [
    ...(header.openWindow.disabledReason === null
      ? [{id: 'window', label: 'Open in new window', press: () => dispatch({type: 'open-window'})}]
      : []),
    ...(header.archive.shown
      ? [{id: 'archive', label: 'Archive', press: () => dispatch({type: 'archive'})}]
      : []),
    {id: 'again', label: 'Run again with changes', press: () => dispatch({type: 'run-again'})},
  ];
  return (
    <Row justify="between" align="start" wrap>
      {/* Grown from no width: the actions keep the title's row and the title's lines wrap instead. */}
      <Stack gap="space-2" grow>
        <Text as="h1" variant="heading">
          {header.title}
        </Text>
        <RunFacts header={header} />
      </Stack>
      <Row gap="space-2">
        {props.toolbar}
        {chat === undefined ? null : (
          <Button
            variant={chat.open ? 'primary' : 'secondary'}
            icon="chat"
            label="Chat"
            expanded={chat.open}
            press={chat.toggle}
            {...(chat.unavailable === undefined ? {} : {disabledReason: chat.unavailable})}
          />
        )}
        {header.pause.shown ? (
          <Button
            variant="secondary"
            text="Pause"
            press={() => dispatch({type: 'pause'})}
            {...reason(header.pause)}
          />
        ) : null}
        {header.resume.shown ? (
          <Button
            variant="primary"
            text="Resume"
            press={() => dispatch({type: 'resume'})}
            {...reason(header.resume)}
          />
        ) : null}
        {header.pauseNow.shown ? (
          <Button
            variant="danger"
            text="Pause now"
            press={() => dispatch({type: 'ask-pause-now'})}
            {...reason(header.pauseNow)}
          />
        ) : null}
        {state.readOnly === true ? null : (
          <Menu icon="more" label="More actions" variant="quiet" align="end" items={items} />
        )}
      </Row>
    </Row>
  );
}

function PauseNowBanner(props: {
  readonly state: RunPageState;
  readonly dispatch: Dispatch;
}): JSX.Element | null {
  const {state, dispatch} = props;
  if (state.readOnly === true) return null;
  const banner = pauseNowBannerView(state);
  if (banner.kind === 'none') return null;
  if (banner.kind === 'confirm') {
    return (
      <Banner
        tone="warn"
        title={banner.title}
        body={banner.body}
        actions={
          <>
            <Button
              variant="danger"
              text="Pause now"
              press={() => dispatch({type: 'confirm-pause-now'})}
            />
            <Button
              variant="secondary"
              text="Keep running"
              press={() => dispatch({type: 'cancel-pause-now'})}
            />
          </>
        }
      />
    );
  }
  return null;
}

function EndedBanner(props: {
  readonly state: RunPageState;
  readonly dispatch: Dispatch;
  readonly zoom: Zoom;
}): JSX.Element | null {
  const {state, dispatch} = props;
  const ended = endedView(state);
  // The header already says Finished. Logs and Run again remain in the run controls.
  if (ended === null || ended.title === 'Finished') return null;
  if (state.readOnly === true) {
    const reason = state.record.kind === 'ended' ? state.record.run.reason : null;
    return reason ? (
      <Banner tone={ended.tone} title={state.lifecycleWord ?? ended.title} body={reason} />
    ) : null;
  }
  return (
    <Banner
      tone={ended.tone}
      title={ended.title}
      body={ended.reason}
      actions={
        <>
          {ended.resume === null ? null : (
            <Button
              variant="primary"
              text={ended.resume}
              press={() => dispatch({type: 'resume-ended'})}
            />
          )}
          <Button
            variant="secondary"
            text="View logs"
            press={() => {
              dispatch({type: 'view-logs'});
              props.zoom({type: 'jump', route: {...RUN_ROUTE, mode: 'logs'}});
            }}
          />
          <Button
            variant="secondary"
            text="Run again"
            press={() => dispatch({type: 'run-again'})}
          />
        </>
      }
    />
  );
}

function Startup(props: {readonly state: RunPageState}): JSX.Element | null {
  const {state} = props;
  if (runPhase(state).kind !== 'starting') return null;
  const stages = timelineView(state).map(stage => {
    const lines = streamLines(state, stage.streamId);
    return {
      // Keyed by state too, so a stage that becomes active or fails opens, as a new one does.
      id: `${stage.id} ${stage.state}`,
      state: stage.state,
      label: stage.label,
      ...(stage.streamId === null || lines.length === 0
        ? {}
        : {body: <LogPane label={`${stage.label} log`} lines={lines} size="sm" />}),
    };
  });
  return (
    <Card section="Starting">
      <Timeline stages={stages} />
    </Card>
  );
}

function crumbs(zoomed: ResolvedZoom, route: Route, zoom: Zoom): Crumb[] {
  const top: Route = {
    mode: route.agent === null ? 'ideas' : 'agents',
    idea: null,
    agent: null,
    step: null,
    item: null,
  };
  const items: {id: string; label: string; mono?: boolean; to: Route}[] = [
    {id: 'run', label: 'Run', to: top},
  ];
  const opened: Route = {...route, step: null, item: null};
  if (zoomed.idea !== null)
    items.push({id: 'idea', label: markdownToPlain(zoomed.idea.title), mono: true, to: opened});
  if (zoomed.agent !== null) {
    items.push({id: 'agent', label: roleLabel(zoomed.agent.role), to: opened});
  }
  if (zoomed.step !== null) {
    const label = stepLabel(zoomed.step);
    items.push({id: 'step', label, to: {...route, item: null}});
  }
  if (zoomed.item !== null) items.push({id: 'item', label: zoomed.item, to: route});
  return items.map(({to, ...item}, index) =>
    index === items.length - 1 ? item : {...item, press: () => zoom({type: 'jump', route: to})},
  );
}

/** The core for an opened step, or the loading/error state of its on-demand read. */
function openedDetail(
  state: RunPageState,
  route: Route,
  core: CoreState | null,
): {core: CoreState | null; loading: HistoryProgress | null; problem: string | null} {
  if (
    state.record.kind !== 'ended' ||
    state.summary.kind !== 'ready' ||
    state.history.kind !== 'idle' ||
    route.step === null
  ) {
    return {core, loading: null, problem: null};
  }
  const detail = state.detail;
  if (detail.kind === 'ready' && detail.spanId === route.step) {
    return {core: detail.core, loading: null, problem: null};
  }
  if (detail.kind === 'failed' && detail.spanId === route.step) {
    return {core, loading: null, problem: detail.message};
  }
  const loading: HistoryProgress =
    detail.kind === 'reading' && detail.spanId === route.step
      ? {events: detail.events.length, bytes: 0, total: detail.total || null}
      : {events: 0, bytes: 0, total: null};
  return {core, loading, problem: null};
}

/** Replace data-dependent panes until the first complete run view is available. */
function pendingRunScreen(
  state: RunPageState,
  dispatch: Dispatch,
  zoom: Zoom,
  route: Route,
  header: ReactNode,
): JSX.Element | null {
  if (state.record.kind === 'unlisted') {
    const blocked = machineBlockWords(state);
    const unplaced = unplacedRun(state);
    return (
      <Screen density="compact" header={header}>
        {blocked !== null ? (
          <RunGone words={blocked} />
        ) : unplaced === null ? (
          <InitialRunLoading stage="locating" />
        ) : (
          <RunUnplaced label={unplaced.label} words={unplaced.words} />
        )}
      </Screen>
    );
  }
  if (
    (state.record.kind === 'ended' || state.record.kind === 'historical') &&
    state.summary.kind === 'reading' &&
    state.run.core === null
  ) {
    return (
      <Screen density="compact" header={header}>
        <InitialRunLoading stage="summary" />
      </Screen>
    );
  }
  const gone = runGoneWords(state);
  if (gone !== null) {
    return (
      <Screen density="compact" header={header}>
        <RunGone words={gone} />
      </Screen>
    );
  }
  const loading = historyLoading(state);
  if (loading !== null) {
    return (
      <Screen density="compact" header={header}>
        <EndedBanner state={state} dispatch={dispatch} zoom={zoom} />
        <HistoryLoading progress={loading} />
      </Screen>
    );
  }
  const problem = historyProblem(state);
  if (
    problem !== null &&
    state.summary.kind !== 'ready' &&
    state.run.core === null &&
    route.mode !== 'logs' &&
    !state.followingLogs
  ) {
    return (
      <Screen density="compact" header={header}>
        <EndedBanner state={state} dispatch={dispatch} zoom={zoom} />
        <RunLoadError message={problem} retry={() => dispatch({type: 'retry-load'})} />
      </Screen>
    );
  }
  return null;
}

/**
 * The run's header, overview, and current level over its summary and selected detail, with the
 * app's chat panel docked at its right edge when the user has opened it.
 */
export function RunScreen(props: {
  readonly state: RunPageState;
  readonly model: RunModel;
  /** The run to its live end, whatever `model`'s sequence: the chart reads this one. */
  readonly whole: RunModel;
  readonly core: CoreState | null;
  readonly route: Route;
  readonly zoom: Zoom;
  readonly dispatch: Dispatch;
  /** The chat panel the app hosts; absent: no Chat button. */
  readonly chat?: RunChatSlot;
  /** The host's names for the run's models; absent or without a model: its id shows. */
  readonly modelNames?: ModelNames;
  /** App-owned run diagnostics, in the page’s scrolling body. */
  readonly toolbar?: ReactNode;
  readonly supplemental?: ReactNode;
}): JSX.Element {
  const {chat, ...rest} = props;
  return (
    <Dock
      label="Chat"
      {...(chat?.minimumMain ? {minimumMain: chat.minimumMain} : {})}
      side={chat?.open === true ? chat.panel : null}
    >
      <RunScreenBody {...rest} chat={chat} />
    </Dock>
  );
}

function RunHeader(
  props: Parameters<typeof Header>[0] & {readonly scrubber: boolean},
): JSX.Element {
  const {scrubber, ...header} = props;
  const {state, dispatch} = props;
  return (
    <Stack gap="space-2">
      <Header {...header} />
      {scrubber ? (
        <RunScrubber
          log={state.run.trace}
          view={state.view}
          ended={runHasEnded(state)}
          wall={runWallNow(state)}
          position={state.positionWord}
          change={view => dispatch({type: 'view', view})}
        />
      ) : null}
    </Stack>
  );
}

function RunScreenBody(props: {
  readonly state: RunPageState;
  readonly model: RunModel;
  /** The run to its live end, whatever `model`'s sequence: the chart reads this one. */
  readonly whole: RunModel;
  readonly core: CoreState | null;
  readonly route: Route;
  readonly zoom: Zoom;
  readonly dispatch: Dispatch;
  readonly chat: RunChatSlot | undefined;
  readonly modelNames?: ModelNames;
  /** App-owned run diagnostics, in the page’s scrolling body. */
  readonly toolbar?: ReactNode;
  readonly supplemental?: ReactNode;
}): JSX.Element {
  const {state, model, route, zoom, dispatch} = props;
  const names = props.modelNames ?? NO_MODEL_NAMES;
  const base: ViewBase = {
    emptyLabelsOnly: state.readOnly === true,
    names,
    model,
    // The run's own start, read from the whole run: a scrubbed model before its first span has no
    // start to give, and every face measures its times from the same origin at every position.
    clock: {start: clockStart(props.whole), now: clockAt(model, runNow(state))},
    tokens: props.core?.tokens ?? EMPTY_LEDGER,
    who: whoBySpan(usageRecords(state), names),
    zoom,
  };
  const pending = pendingRunScreen(
    state,
    dispatch,
    zoom,
    route,
    <Header
      state={state}
      names={names}
      dispatch={dispatch}
      chat={props.chat}
      toolbar={props.toolbar}
    />,
  );
  if (pending !== null) return pending;
  const problem = historyProblem(state);
  const transcript = transcriptLoading(state);
  const zoomed = resolveZoom(model, route);
  const before = roundsBeforeTrace(state);
  const detail = openedDetail(state, route, props.core);
  // The chain lit on the chart and in the tree: what the pointer is on, else what was pinned or opened.
  const lit = ideaAncestry(
    model,
    state.ideasHovered ?? state.ideasPinned ?? zoomed.idea?.id ?? null,
  );
  const deep = routeLevel(route) !== 'run' && (zoomed.idea !== null || zoomed.agent !== null);
  const level = {
    state,
    base,
    core: detail.core,
    mode: route.mode,
    dispatch,
    loading: detail.loading ?? transcript,
  };
  return (
    <Screen
      density="compact"
      header={
        <RunHeader
          state={state}
          names={names}
          dispatch={dispatch}
          chat={props.chat}
          toolbar={props.toolbar}
          scrubber={traceArrival(state) === 'arrived' && before === null}
        />
      }
    >
      <PauseNowBanner state={state} dispatch={dispatch} />
      {state.notice === null ? null : (
        <Banner tone="err" title="The run did not take the command" body={state.notice} />
      )}
      <EndedBanner state={state} dispatch={dispatch} zoom={zoom} />
      {problem === null ? null : (
        <Banner tone="warn" title="The run’s history is not available" body={problem} />
      )}
      <Startup state={state} />
      {before === null ? (
        <Hero
          {...base}
          whole={props.whole}
          idea={zoomed.idea?.id ?? null}
          lit={lit}
          hover={idea => dispatch({type: 'ideas-hover', idea})}
        />
      ) : (
        <Banner title={predatesTraceWords(before).title} body={predatesTraceWords(before).body} />
      )}
      {showsBreadcrumb(route, zoomed) ? (
        <Breadcrumb label="Zoom" items={crumbs(zoomed, route, zoom)} />
      ) : (
        <Reserve>
          <Breadcrumb label="Zoom" items={BREADCRUMB_ROW} />
        </Reserve>
      )}
      {detail.problem === null ? null : (
        <Banner tone="warn" title="The step’s transcript is not available" body={detail.problem} />
      )}
      <LevelBody deep={deep} route={route} zoomed={zoomed} level={level} />
      {props.supplemental}
    </Screen>
  );
}
