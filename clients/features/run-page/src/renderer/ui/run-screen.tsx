import {
  Banner,
  Button,
  Card,
  Chip,
  EmptyState,
  LogPane,
  Menu,
  type MenuItem,
  Page,
  Row,
  Split,
  Stack,
  Tabs,
  Text,
  Timeline,
  Tree,
  TreeRow,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  endedView,
  headerView,
  logGroups,
  logsNotice,
  NOTHING_FOLDED,
  type Offer,
  quotaBannerView,
  RUN_TABS,
  type RunAtView,
  type RunPageAction,
  type RunPageState,
  type RunTab,
  runPhase,
  shownStream,
  spanLogs,
  stopBannerView,
  streamLines,
  timelineView,
  viewedRun,
} from '../../state/index.js';
import {MapTab} from './map-tab.js';
import {OverviewTab} from './overview-tab.js';
import {RunScrubber} from './run-scrubber.js';
import {WorkTab} from './work-tab.js';

type Dispatch = (action: RunPageAction) => void;

const TAB_LABELS: Readonly<Record<RunTab, string>> = {
  overview: 'Overview',
  map: 'Map',
  work: 'Work',
  logs: 'Logs',
};

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
        <Text as="h1" variant="title">
          {header.title}
        </Text>
        <Row gap="space-3" wrap>
          <Chip label={header.status.label} tone={header.status.tone} dot />
          <Text variant="code" tone="muted">
            {header.subtitle}
          </Text>
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
            press={() => dispatch({type: 'view-logs'})}
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

function LogsTab(props: {readonly state: RunPageState; readonly dispatch: Dispatch}): JSX.Element {
  const {state, dispatch} = props;
  const notice = logsNotice(state);
  const groups = logGroups(state);
  const shown = shownStream(state);
  if (notice !== null && groups.length === 0) {
    return (
      <Card>
        <EmptyState title="No logs to show" body={notice} />
      </Card>
    );
  }
  const label =
    groups.flatMap(group => group.streams).find(stream => stream.id === shown)?.label ?? 'Log';
  return (
    <Split
      main={
        <Card section={label}>
          <LogPane label={label} lines={streamLines(state, shown)} />
        </Card>
      }
      aside={
        <Card section="Streams">
          <Tree label="Log streams" {...(shown === null ? {} : {selected: shown})}>
            {groups.flatMap(group => [
              <TreeRow
                key={`group ${group.name}`}
                id={`group ${group.name}`}
                depth={0}
                title={group.name}
                expanded
                select={() => {
                  const first = group.streams[0];
                  if (first !== undefined) dispatch({type: 'select-stream', streamId: first.id});
                }}
              />,
              ...group.streams.map(stream => (
                <TreeRow
                  key={stream.id}
                  id={stream.id}
                  depth={1}
                  title={stream.label}
                  state={
                    stream.live
                      ? {tone: 'live', label: 'Writing'}
                      : {tone: 'neutral', label: 'Closed'}
                  }
                  select={() => dispatch({type: 'select-stream', streamId: stream.id})}
                />
              )),
            ])}
          </Tree>
        </Card>
      }
    />
  );
}

/** Whether the run's trace has reached the window: a snapshot, or a span event, folded. */
function traceReached(state: RunPageState): boolean {
  const {trace} = state.run;
  return state.session?.hasSnapshot === true || trace.base.sequence > 0 || trace.events.length > 0;
}

function Panel(props: {
  readonly state: RunPageState;
  readonly run: RunAtView;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {state, run, dispatch} = props;
  switch (state.tab) {
    case 'logs':
      return <LogsTab state={state} dispatch={dispatch} />;
    case 'overview':
      return <OverviewTab run={run} state={state} />;
    case 'work':
      return <WorkTab run={run} work={state.work} dispatch={dispatch} />;
    case 'map':
      return (
        <MapTab
          trace={traceReached(state) ? viewedRun(state).trace : null}
          map={state.map}
          logsOf={spanId => spanLogs(state, spanId)}
          actions={{
            filter: filter => dispatch({type: 'map-filter', filter}),
            toggle: (id, open) => dispatch({type: 'map-toggle', id, open}),
            select: id => dispatch({type: 'map-select', id}),
            openLog: streamId => dispatch({type: 'select-stream', streamId}),
          }}
        />
      );
  }
}

/**
 * The Run screen: the header with its actions, the stop and ended banners, the startup timeline
 * while the run starts, the time scrubber once the run's trace has reached the window, and the
 * tabs. Everything shown is a selector over `state` and `run`, the folded run at the view (nothing
 * folded until the live connection provides it); the Map and the scrubber read the trace log the
 * state holds.
 */
export function RunScreen(props: {
  readonly state: RunPageState;
  readonly run?: RunAtView;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {state, dispatch} = props;
  const run = props.run ?? NOTHING_FOLDED;
  return (
    <Page width="wide">
      <Stack gap="space-5">
        <Header state={state} dispatch={dispatch} />
        <StopBanner state={state} dispatch={dispatch} />
        <QuotaBanner state={state} dispatch={dispatch} />
        {state.notice === null ? null : (
          <Banner tone="err" title="The run did not take the command" body={state.notice} />
        )}
        <EndedBanner state={state} dispatch={dispatch} />
        <Startup state={state} />
        {traceReached(state) ? (
          <RunScrubber
            log={state.run.trace}
            view={state.view}
            ended={runPhase(state).kind === 'ended'}
            change={view => dispatch({type: 'view', view})}
          />
        ) : null}
        <Tabs
          label="Run views"
          tabs={RUN_TABS.map(tab => ({value: tab, label: TAB_LABELS[tab]}))}
          value={state.tab}
          change={tab => dispatch({type: 'tab', tab})}
        >
          <Panel state={state} run={run} dispatch={dispatch} />
        </Tabs>
      </Stack>
    </Page>
  );
}
