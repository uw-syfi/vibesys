/** The app chooses one reader for a run, using the existing host setting. */

import {RunIntents} from '@vibesys/feature-run-intents/renderer';
import type {RunPageAddress} from '@vibesys/feature-run-page/renderer';
import {type GatewayRunPorts, RunScreen, useGatewayRun} from '@vibesys/feature-run-page/renderer';
import type {RunPageSeed} from '@vibesys/feature-run-page/state';
import type {GatewayBridge, WelcomeBridge} from '@vibesys/ipc';
import type {ScheduleTimeout} from '@vibesys/machine';
import type {Skeleton} from '@vibesys/run-skeleton';
import {gatewayContentCache} from '@vibesys/run-skeleton/cache';
import {gatewayReader} from '@vibesys/run-skeleton/reader';
import type {RelaunchObservation} from '@vibesys/runs/intents';
import {Card, EmptyState, Page, Switch, Text} from '@vibesys/ui';
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
  const busy = snapshot.matches('checking') || snapshot.matches('saving');
  const toggle = (
    <Switch
      label="Run transport v2"
      checked={snapshot.context.enabled}
      change={page.toggle}
      {...(busy ? {disabledReason: 'Connecting'} : {})}
    />
  );
  if (snapshot.matches('off')) return <>{props.legacy(<Card>{toggle}</Card>)}</>;
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
  const observation = observationOf(snapshot.context.load?.skeleton);
  return (
    <RunScreen
      state={page.state}
      model={page.model}
      whole={page.whole}
      core={page.state.run.core}
      route={page.route}
      zoom={page.zoom}
      dispatch={page.dispatch}
      toolbar={
        <>
          {toggle}
          {page.state.record.kind === 'live' || page.state.record.kind === 'ended' ? (
            <RunIntents
              key={`${seed.identity.host}/${seed.identity.runId}`}
              bridge={bridge}
              host={seed.identity.host}
              runId={seed.identity.runId}
              projectRoot={page.state.record.run.projectRoot}
              enabled={snapshot.context.enabled}
              schedule={schedule}
              {...(observation ? {relaunch: observation} : {})}
            />
          ) : null}
        </>
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
