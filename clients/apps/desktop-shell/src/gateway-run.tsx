/** The app chooses one reader for a run, using the existing host setting. */

import {useRunIntents} from '@vibesys/feature-run-intents/renderer';
import type {RunPageAddress} from '@vibesys/feature-run-page/renderer';
import {type GatewayRunPorts, RunScreen, useGatewayRun} from '@vibesys/feature-run-page/renderer';
import type {RunPageSeed} from '@vibesys/feature-run-page/state';
import {useRunSessions} from '@vibesys/feature-run-sessions/renderer';
import type {GatewayBridge, WelcomeBridge} from '@vibesys/ipc';
import type {ScheduleTimeout} from '@vibesys/machine';
import type {Skeleton} from '@vibesys/run-skeleton';
import {gatewayContentCache} from '@vibesys/run-skeleton/cache';
import {gatewayReader} from '@vibesys/run-skeleton/reader';
import {proposalIntent} from '@vibesys/runs/chat';
import type {RelaunchObservation} from '@vibesys/runs/intents';
import {Card, EmptyState, Page, Stack, Switch, Text} from '@vibesys/ui';
import {type JSX, type ReactNode, useMemo} from 'react';
import {gatewayTransport} from './gateway-transport.js';

export function GatewayRun(props: {
  readonly seed: RunPageSeed;
  readonly bridge: GatewayBridge;
  readonly settings: WelcomeBridge;
  readonly schedule: ScheduleTimeout;
  readonly address: RunPageAddress;
  readonly legacy: (toggle: ReactNode) => ReactNode;
}): JSX.Element {
  const {bridge, settings, seed, schedule, address} = props;
  const ports = useMemo<GatewayRunPorts>(
    () => ({
      watchSetting: listener => settings.onHostSettingsChanged(listener),
      reader: gatewayReader(
        gatewayTransport(bridge, seed.identity.host),
        gatewayContentCache(indexedDB),
        schedule,
      ),
      setting: async enabled => {
        const reply = await settings.runTransportV2(seed.identity.host, enabled);
        if (!reply.ok) throw new Error(reply.error);
        return reply.value;
      },
    }),
    [bridge, settings, seed.identity.host, schedule],
  );
  const page = useGatewayRun({seed, ports, schedule, address});
  const {snapshot} = page;
  const observation = observationOf(snapshot.context.load?.skeleton);
  const record = page.state?.record;
  const projectRoot =
    record?.kind === 'live' || record?.kind === 'ended' ? record.run.projectRoot : '';
  const intents = useRunIntents({
    bridge,
    host: seed.identity.host,
    runId: seed.identity.runId,
    projectRoot,
    ...(observation ? {relaunch: observation} : {}),
    enabled: snapshot.matches('active') && projectRoot !== '',
    schedule,
  });
  const sessions = useRunSessions({
    bridge,
    host: seed.identity.host,
    runId: seed.identity.runId,
    sessionId: 'default',
    projectRoot,
    enabled: snapshot.matches('active') && projectRoot !== '',
    schedule,
    steerBusy: intents.busy,
    sendSteer: proposal =>
      intents.sendRequest({
        type: 'send',
        request: proposalIntent(
          {projectRoot, runId: seed.identity.runId, requestId: crypto.randomUUID()},
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
          <EmptyState title={snapshot.context.error ?? page.projectionError ?? 'Loading'} />
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
        snapshot.context.error ? <Text tone="err">{snapshot.context.error}</Text> : null
      }
    />
  );
}

function observationOf(skeleton: Skeleton | undefined): RelaunchObservation | undefined {
  const latest = skeleton?.epochs.at(-1);
  if (!latest?.epoch) return undefined;
  const lifecycle = skeleton?.lifecycles.at(-1);
  return {
    epoch: BigInt(latest.epoch.epoch),
    ...(lifecycle?.lifecycle && lifecycle.seq >= latest.seq
      ? {lifecycle: lifecycle.lifecycle}
      : {}),
  };
}
