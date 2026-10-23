import {newRequestId} from '@vibesys/request-identity';
/** The app chooses one reader for a run, using the existing host setting. */

import {useRunIntents} from '@vibesys/feature-run-intents/renderer';
import type {RunPageAddress} from '@vibesys/feature-run-page/renderer';
import {
  type GatewayRunPorts,
  LoadStatus,
  RunScreen,
  useGatewayRun,
} from '@vibesys/feature-run-page/renderer';
import {type RunPageSeed, recordedRunRoot} from '@vibesys/feature-run-page/state';
import {useRunSessions} from '@vibesys/feature-run-sessions/renderer';
import type {GatewayBridge, WelcomeBridge} from '@vibesys/ipc';
import {gatewayTransport} from '@vibesys/ipc';
import type {ScheduleTimeout} from '@vibesys/machine';
import {
  observeRun,
  type RecordsFanout,
  type RecordsListener,
  recordsFanout,
  type Skeleton,
} from '@vibesys/run-skeleton';
import {gatewayContentCache} from '@vibesys/run-skeleton/cache';
import {type GatewayReader, gatewayReader} from '@vibesys/run-skeleton/reader';
import {proposalIntent} from '@vibesys/runs/chat';
import {type RelaunchObservation, runReference} from '@vibesys/runs/intents';
import {Card, EmptyState, Page, Stack, Switch, Text} from '@vibesys/ui';
import {type JSX, type ReactNode, useMemo} from 'react';

export function GatewayRun(props: {
  readonly seed: RunPageSeed;
  readonly bridge: GatewayBridge;
  readonly settings: WelcomeBridge;
  readonly schedule: ScheduleTimeout;
  readonly address: RunPageAddress;
  readonly legacy: (toggle: ReactNode) => ReactNode;
}): JSX.Element {
  const {bridge, settings, seed, schedule, address} = props;
  const {fanout, ports} = useMemo(
    () => gatewayPorts({bridge, settings, host: seed.identity.host, schedule}),
    [bridge, settings, seed.identity.host, schedule],
  );
  const page = useGatewayRun({seed, ports, schedule, address});
  const {snapshot} = page;
  const observation = observationOf(snapshot.context.load?.skeleton);
  const projectRoot = recordedRunRoot(snapshot.context.load?.skeleton);
  const intents = useRunIntents({
    bridge,
    host: seed.identity.host,
    runId: seed.identity.runId,
    records: fanout.subscribe,
    projectRoot,
    ...(observation ? {relaunch: observation} : {}),
    enabled: snapshot.context.enabled,
    schedule,
  });
  const sessions = useRunSessions({
    bridge,
    host: seed.identity.host,
    runId: seed.identity.runId,
    sessionId: 'default',
    projectRoot: projectRoot ?? '',
    enabled: snapshot.matches('active') && projectRoot !== undefined,
    schedule,
    steerBusy: intents.busy,
    sendSteer: proposal =>
      intents.sendRequest({
        type: 'send',
        request: proposalIntent(
          {
            ...runReference(projectRoot ?? '', seed.identity.runId),
            requestId: newRequestId(),
            expectedEpoch: observation?.epoch ?? 0n,
          },
          proposal,
        ),
      }),
  });
  const busy = snapshot.matches('checking') || snapshot.matches('saving');
  const toggle = (
    <Switch
      label="Run transport v2"
      checked={snapshot.context.enabled}
      change={page.toggle}
      {...(busy ? {disabledReason: 'Connecting'} : {})}
    />
  );
  if (snapshot.matches('off')) return <>{props.legacy(toggle)}</>;
  if (
    page.state === null ||
    page.model === null ||
    page.whole === null ||
    !snapshot.matches('active')
  ) {
    return (
      <Page>
        <Card>{toggle}</Card>
        <Card>
          {(snapshot.context.error ?? page.projectionError) ? (
            <EmptyState title={snapshot.context.error ?? page.projectionError ?? ''} />
          ) : (
            <LoadStatus view={page.load} retry={page.retry} />
          )}
        </Card>
      </Page>
    );
  }
  return (
    <RunScreen
      state={page.state}
      model={page.model}
      whole={page.whole}
      core={page.state.run.core}
      route={page.route}
      zoom={page.zoom}
      dispatch={page.dispatch}
      chat={{...sessions, minimumMain: 'wide'}}
      toolbar={
        <Stack gap="space-1" align="start">
          {toggle}
          {intents.controls}
        </Stack>
      }
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
  readonly settings: WelcomeBridge;
  readonly host: RunPageSeed['identity']['host'];
  readonly schedule: ScheduleTimeout;
}): {readonly fanout: RecordsFanout; readonly ports: GatewayRunPorts} {
  const {bridge, settings, host, schedule} = input;
  const fanout = recordsFanout();
  const reader = gatewayReader(
    gatewayTransport(bridge, host),
    gatewayContentCache(indexedDB),
    schedule,
  );
  return {
    fanout,
    ports: {
      watchSetting: listener => settings.onHostSettingsChanged(listener),
      reader: shared(reader, fanout.publish),
      setting: async enabled => {
        const reply = await settings.runTransportV2(host, enabled);
        if (!reply.ok) throw new Error(reply.error);
        return reply.value;
      },
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

function observationOf(skeleton: Skeleton | undefined): RelaunchObservation | undefined {
  if (!skeleton) return undefined;
  const {epoch, lifecycle} = observeRun(skeleton);
  if (epoch === 0) return undefined;
  return {epoch: BigInt(epoch), ...(lifecycle ? {lifecycle} : {})};
}
