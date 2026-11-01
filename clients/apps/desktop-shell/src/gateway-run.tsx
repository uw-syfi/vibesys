/** The gateway owns every run reader. */

import {useRunIntents} from '@vibesys/feature-run-intents/renderer';
import type {RunPageAddress, RunPageNavigation} from '@vibesys/feature-run-page/renderer';
import {
  type GatewayRunPorts,
  LoadStatus,
  RunScreen,
  useGatewayRun,
} from '@vibesys/feature-run-page/renderer';
import {type RunPageSeed, runPageProblems} from '@vibesys/feature-run-page/state';
import type {RecordedProjectRoot} from '@vibesys/hosts';
import type {GatewayBridge} from '@vibesys/ipc';
import type {ScheduleTimeout} from '@vibesys/machine';
import type {ProblemBoard} from '@vibesys/problems';
import {type RecordsFanout, type RecordsListener, recordsFanout} from '@vibesys/run-skeleton';
import {gatewayContentCache} from '@vibesys/run-skeleton/cache';
import {type GatewayReader, gatewayReader} from '@vibesys/run-skeleton/reader';
import type {SubscriptionPort} from '@vibesys/subscription';
import {Card, EmptyState, Page, pageState, Text} from '@vibesys/ui';
import {type JSX, useMemo} from 'react';
import {useGatewayChat} from './gateway-chat.js';
import {gatewayTransport} from './gateway-transport.js';
import {useReportedProblems} from './problem-host.js';
import {runLiveness} from './run-liveness.js';

interface GatewayRunProps {
  readonly seed: RunPageSeed;
  readonly projectRoot: RecordedProjectRoot | undefined;
  readonly bridge: GatewayBridge;
  /** The window's port to the host's live data, which this run's liveness is read through. */
  readonly subscriptions: SubscriptionPort;
  readonly schedule: ScheduleTimeout;
  readonly address: RunPageAddress;
  readonly navigation: RunPageNavigation;
  /** Where this run's problems go while its screen is up. */
  readonly problems: ProblemBoard;
}

export function GatewayRun(props: GatewayRunProps): JSX.Element {
  const {bridge, subscriptions, seed, schedule, address} = props;
  const {fanout, ports} = useMemo(
    () => gatewayPorts({bridge, subscriptions, host: seed.identity.host, schedule}),
    [bridge, subscriptions, seed.identity.host, schedule],
  );
  const page = useGatewayRun({seed, ports, schedule, address});
  const {snapshot} = page;
  const reported = useMemo(
    () => (page.state === null ? [] : runPageProblems(page.state)),
    [page.state],
  );
  useReportedProblems(props.problems, `run ${seed.identity.host} ${seed.identity.runId}`, reported);
  const {observation} = page.writer;
  const projectRoot = props.projectRoot;
  const intents = useRunIntents({
    bridge,
    host: seed.identity.host,
    runId: seed.identity.runId,
    projectRoot,
    enabled: true,
    records: fanout.subscribe,
    refreshWriter: page.refreshWriter,
    writer: page.writer,
    schedule,
  });
  const sessions = useGatewayChat({
    bridge,
    host: seed.identity.host,
    runId: seed.identity.runId,
    projectRoot: projectRoot ?? '',
    schedule,
    records: fanout.subscribe,
    core: page.state?.run.core ?? null,
    epoch: observation?.epoch ?? 0n,
    readOnly: !intents.steerable,
  });
  if (
    page.state === null ||
    page.model === null ||
    page.whole === null ||
    !snapshot.matches('active')
  ) {
    // Until the run is shown the page waits for it, unless it has said why it cannot be.
    const error = snapshot.context.error ?? page.projectionError;
    return (
      <Page state={pageState(!error && page.load.kind !== 'failed')}>
        <Card>
          {error ? (
            <EmptyState title={error} />
          ) : (
            <LoadStatus view={page.load} retry={page.retry} />
          )}
        </Card>
      </Page>
    );
  }
  // A Resume the controls asked for is the run's state until the host answers, as the page's own is.
  const state = intents.resuming
    ? {...page.state, resumeStatus: {pending: true as const, reason: 'Waiting for the host'}}
    : page.state;
  return (
    <RunScreen
      state={state}
      model={page.model}
      whole={page.whole}
      core={page.state.run.core}
      route={page.route}
      zoom={page.zoom}
      dispatch={action => {
        if (action.type === 'run-again') props.navigation.runAgain();
        else if (action.type === 'open-window') props.navigation.openWindow();
        else page.dispatch(action);
      }}
      chat={sessions}
      loading={page.load.kind === 'loading' || page.load.kind === 'reconnecting'}
      toolbar={intents.controls}
      {...(props.navigation.runs === undefined ? {} : {runs: props.navigation.runs})}
      supplemental={
        <>
          <LoadStatus view={page.load} retry={page.retry} />
          {snapshot.context.error ? <Text tone="err">{snapshot.context.error}</Text> : null}
        </>
      }
    />
  );
}

function gatewayPorts(input: {
  readonly bridge: GatewayBridge;
  readonly subscriptions: SubscriptionPort;
  readonly host: RunPageSeed['identity']['host'];
  readonly schedule: ScheduleTimeout;
}): {readonly fanout: RecordsFanout; readonly ports: GatewayRunPorts} {
  const {bridge, subscriptions, host, schedule} = input;
  const fanout = recordsFanout();
  const reader = gatewayReader(
    gatewayTransport(bridge, host),
    gatewayContentCache(indexedDB),
    schedule,
    runLiveness(subscriptions, host),
  );
  return {
    fanout,
    ports: {
      reader: shared(reader, fanout.publish),
    },
  };
}

/** The one reader of the run's log also feeds every other consumer of its records. */
function shared(reader: GatewayReader, publish: RecordsListener): GatewayReader {
  return {
    ...reader,
    watch: (runId, receive, records) =>
      reader.watch(runId, receive, batch => {
        records(batch);
        publish(batch);
      }),
  };
}
