import {type CoreState, EMPTY_LEDGER} from '@vibesys/run-state';
import {
  Banner,
  Breadcrumb,
  Button,
  Card,
  Chip,
  type Crumb,
  LogPane,
  Menu,
  type MenuItem,
  Row,
  Screen,
  Stack,
  Text,
  Timeline,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  clockAt,
  clockCarried,
  clockStart,
  endedView,
  headerView,
  ideaAncestry,
  type Offer,
  quotaBannerView,
  type ResolvedZoom,
  type Route,
  RUN_ROUTE,
  type RunModel,
  type RunPageAction,
  type RunPageState,
  resolveZoom,
  routeLevel,
  runNow,
  runPhase,
  stopBannerView,
  streamLines,
  timelineView,
  traceArrival,
} from '../../state/index.js';
import {Hero} from './hero.js';
import {DeepLevel} from './levels.js';
import {RunLevel} from './run-level.js';
import {RunScrubber} from './run-scrubber.js';
import type {ViewBase, Zoom} from './views/props.js';

type Dispatch = (action: RunPageAction) => void;

function reason(offer: Offer): {disabledReason?: string} {
  return offer.disabledReason === null ? {} : {disabledReason: offer.disabledReason};
}

function Header(props: {readonly state: RunPageState; readonly dispatch: Dispatch}): JSX.Element {
  const {state, dispatch} = props;
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
      <Stack gap="space-2">
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
              {`· ${header.spent.label} tokens`}
            </Text>
          )}
        </Row>
      </Stack>
      <Row gap="space-3">
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
          <Button variant="danger" text="Stop" press={() => dispatch({type: 'ask-stop'})} />
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
  if (ended === null) return null;
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
  const items: {id: string; label: string; to: Route}[] = [{id: 'run', label: 'Run', to: top}];
  const opened: Route = {...route, step: null, item: null};
  if (zoomed.idea !== null) items.push({id: 'idea', label: zoomed.idea.title, to: opened});
  if (zoomed.agent !== null) items.push({id: 'agent', label: zoomed.agent.role, to: opened});
  if (zoomed.step !== null) {
    const label = zoomed.step.role ?? zoomed.step.title;
    items.push({id: 'step', label, to: {...route, item: null}});
  }
  if (zoomed.item !== null) items.push({id: 'item', label: zoomed.item, to: route});
  return items.map((item, index) =>
    index === items.length - 1
      ? {id: item.id, label: item.label}
      : {id: item.id, label: item.label, press: () => zoom({type: 'jump', route: item.to})},
  );
}

/**
 * The Run screen: a fixed page whose header holds the run's title, status, facts and controls and
 * the one-line time bar, then the banners, the performance row (the hero and its chart: the same
 * element at every level, singling out the open idea) and the level the route names: the run (the
 * Ideas, Timeline, Agents or Logs face, and Now) or a level below it, at most two side by side. `model` is the run at the view's sequence (`createRunReader`); `route` is the zoom
 * (`zoomMachine`), which `zoom` moves.
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
}): JSX.Element {
  const {state, model, route, zoom, dispatch} = props;
  const base: ViewBase = {
    model,
    clock: {start: clockStart(model), now: clockAt(model, runNow(state))},
    tokens: props.core?.tokens ?? EMPTY_LEDGER,
    zoom,
  };
  const zoomed = resolveZoom(model, route);
  // The chain lit on the chart and in the tree: what the pointer is on, else what was pinned or opened.
  const lit = ideaAncestry(
    model,
    state.ideasHovered ?? state.ideasPinned ?? zoomed.idea?.id ?? null,
  );
  const deep = routeLevel(route) !== 'run' && (zoomed.idea !== null || zoomed.agent !== null);
  const level = {state, base, core: props.core, mode: route.mode, dispatch};
  return (
    <Screen
      density="compact"
      header={
        <Stack gap="space-2">
          <Header state={state} dispatch={dispatch} />
          {traceArrival(state) === 'arrived' ? (
            <RunScrubber
              log={state.run.trace}
              view={state.view}
              ended={runPhase(state).kind === 'ended'}
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
      <Startup state={state} />
      <Hero
        {...base}
        whole={props.whole}
        idea={zoomed.idea?.id ?? null}
        lit={lit}
        hover={idea => dispatch({type: 'ideas-hover', idea})}
      />
      {deep ? <Breadcrumb label="Zoom" items={crumbs(zoomed, route, zoom)} /> : null}
      {deep ? <DeepLevel {...level} zoomed={zoomed} /> : <RunLevel {...level} />}
    </Screen>
  );
}
