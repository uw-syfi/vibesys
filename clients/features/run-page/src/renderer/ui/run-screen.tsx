import {type CoreState, EMPTY_LEDGER} from '@vibesys/run-state';
import {
  Banner,
  Breadcrumb,
  Button,
  Card,
  Chip,
  type Crumb,
  Dock,
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
  clockCarried,
  clockStart,
  endedView,
  type HistoryProgress,
  headerView,
  historyLoading,
  historyProblem,
  ideaAncestry,
  machineBlockWords,
  type Offer,
  predatesTraceWords,
  quotaBannerView,
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
  stepLabel,
  stopBannerView,
  streamLines,
  timelineView,
  traceArrival,
  transcriptLoading,
  unplacedRun,
  usageRecords,
  whoBySpan,
} from '../../state/index.js';
import {GateFailures} from './gate-failures.js';
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
  readonly open: boolean;
  readonly toggle: () => void;
  readonly panel: ReactNode;
}

function Header(props: {
  readonly state: RunPageState;
  readonly dispatch: Dispatch;
  readonly chat: RunChatSlot | undefined;
}): JSX.Element {
  const {state, dispatch, chat} = props;
  const header = headerView(state);
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
        <Row gap="space-3" wrap>
          <Chip label={header.status.label} tone={header.status.tone} dot />
          <Text variant="code" tone="muted">
            {header.subtitle}
          </Text>
          {header.spent === null ? null : (
            <Text variant="code" tone="muted" title={header.spent.title}>
              {`· ${header.spent.label}`}
            </Text>
          )}
        </Row>
        {header.agents.length === 0 ? null : (
          <Row gap="space-4" wrap>
            {header.agents.map(agent => (
              <Row key={agent.role} gap="space-2" align="baseline">
                <Text variant="small" tone="muted">
                  {agent.role}
                </Text>
                <Text variant="code">{agent.ran.join(', ')}</Text>
              </Row>
            ))}
          </Row>
        )}
      </Stack>
      <Row gap="space-2">
        {chat === undefined ? null : (
          <Button
            variant={chat.open ? 'primary' : 'secondary'}
            icon="chat"
            label="Chat"
            expanded={chat.open}
            press={chat.toggle}
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
        {header.stop.shown ? (
          <Button
            variant="danger"
            text="Stop"
            press={() => dispatch({type: 'ask-stop'})}
            {...reason(header.stop)}
          />
        ) : null}
        <Menu icon="more" label="More actions" variant="quiet" align="end" items={items} />
      </Row>
    </Row>
  );
}

function StopBanner(props: {
  readonly state: RunPageState;
  readonly dispatch: Dispatch;
}): JSX.Element | null {
  const {state, dispatch} = props;
  const banner = stopBannerView(state);
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
              text="Stop run"
              press={() => dispatch({type: 'confirm-stop'})}
            />
            <Button
              variant="secondary"
              text="Keep running"
              press={() => dispatch({type: 'cancel-stop'})}
            />
          </>
        }
      />
    );
  }
  return (
    <Banner
      tone="warn"
      title={banner.title}
      body={banner.body}
      {...(banner.forceOffered
        ? {
            actions: (
              <Button
                variant="danger"
                text="Force stop"
                press={() => dispatch({type: 'force-stop'})}
              />
            ),
          }
        : {})}
    />
  );
}

/** Words an epoch second as the user's local weekday and time; reads no clock. */
const QUOTA_TIME = new Intl.DateTimeFormat(undefined, {
  weekday: 'short',
  hour: '2-digit',
  minute: '2-digit',
});

function QuotaBanner(props: {
  readonly state: RunPageState;
  readonly dispatch: Dispatch;
}): JSX.Element | null {
  const {state, dispatch} = props;
  const banner = quotaBannerView(state, seconds => QUOTA_TIME.format(seconds * 1000));
  if (banner === null) return null;
  return (
    <Banner
      tone="warn"
      title={banner.title}
      body={banner.body}
      actions={
        <>
          <Button
            variant="primary"
            text="Resume now"
            press={() => dispatch({type: 'resume'})}
            {...reason(banner.resumeNow)}
          />
          {banner.wait.shown ? (
            <Button
              variant="secondary"
              text="Wait and resume when quota is back"
              press={() => dispatch({type: 'wait-for-quota'})}
              {...reason(banner.wait)}
            />
          ) : null}
          {banner.stopWaiting.shown ? (
            <Button
              variant="secondary"
              text="Stop waiting"
              press={() => dispatch({type: 'stop-waiting'})}
              {...reason(banner.stopWaiting)}
            />
          ) : null}
        </>
      }
    />
  );
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
  chat: RunChatSlot | undefined,
): JSX.Element | null {
  const header = <Header state={state} dispatch={dispatch} chat={chat} />;
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
}): JSX.Element {
  const {chat, ...rest} = props;
  return (
    <Dock label="Chat" side={chat?.open === true ? chat.panel : null}>
      <RunScreenBody {...rest} chat={chat} />
    </Dock>
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
}): JSX.Element {
  const {state, model, route, zoom, dispatch} = props;
  const base: ViewBase = {
    model,
    clock: {start: clockStart(model), now: clockAt(model, runNow(state))},
    tokens: props.core?.tokens ?? EMPTY_LEDGER,
    who: whoBySpan(usageRecords(state)),
    zoom,
  };
  const pending = pendingRunScreen(state, dispatch, zoom, route, props.chat);
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
        <Stack gap="space-2">
          <Header state={state} dispatch={dispatch} chat={props.chat} />
          {traceArrival(state) === 'arrived' && before === null ? (
            <RunScrubber
              log={state.run.trace}
              view={state.view}
              ended={runHasEnded(state)}
              carried={clockCarried(model, runNow(state))}
              change={view => dispatch({type: 'view', view})}
            />
          ) : null}
        </Stack>
      }
    >
      <StopBanner state={state} dispatch={dispatch} />
      <QuotaBanner state={state} dispatch={dispatch} />
      {state.notice === null ? null : (
        <Banner tone="err" title="The run did not take the command" body={state.notice} />
      )}
      <EndedBanner state={state} dispatch={dispatch} zoom={zoom} />
      <GateFailures model={props.whole} />
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
    </Screen>
  );
}
