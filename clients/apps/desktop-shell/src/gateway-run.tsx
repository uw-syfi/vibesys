/** The gateway owns every run reader. */

import {useRunIntents} from '@vibesys/feature-run-intents/renderer';
import type {RunPageAddress, RunPageNavigation} from '@vibesys/feature-run-page/renderer';
import {
  type GatewayRunPorts,
  LoadStatus,
  RunScreen,
  useGatewayRun,
} from '@vibesys/feature-run-page/renderer';
import type {ErrorReport, RunPageSeed} from '@vibesys/feature-run-page/state';
import type {RecordedProjectRoot} from '@vibesys/hosts';
import type {GatewayBridge} from '@vibesys/ipc';
import type {ScheduleTimeout} from '@vibesys/machine';
import {type RecordsFanout, type RecordsListener, recordsFanout} from '@vibesys/run-skeleton';
import {gatewayContentCache} from '@vibesys/run-skeleton/cache';
import {type GatewayReader, gatewayReader} from '@vibesys/run-skeleton/reader';
import {Card, EmptyState, ErrorActions, Page, Text} from '@vibesys/ui';
import {type JSX, useMemo, useRef} from 'react';
import {useGatewayChat} from './gateway-chat.js';
import {gatewayTransport} from './gateway-transport.js';

interface GatewayRunProps {
  readonly seed: RunPageSeed;
  readonly projectRoot: RecordedProjectRoot | undefined;
  readonly bridge: GatewayBridge;
  readonly schedule: ScheduleTimeout;
  readonly address: RunPageAddress;
  readonly navigation: RunPageNavigation;
}

export function GatewayRun(props: GatewayRunProps): JSX.Element {
  const {bridge, seed, schedule, address} = props;
  const {fanout, ports} = useMemo(
    () => gatewayPorts({bridge, host: seed.identity.host, schedule}),
    [bridge, seed.identity.host, schedule],
  );
  const page = useGatewayRun({seed, ports, schedule, address});
  const {snapshot} = page;
  const {observation} = page.writer;
  const projectRoot = props.projectRoot;
  // The chat is made after the controls (its readOnly reads `intents.steerable`), so the controls
  // reach its asker through a ref set below, as `use-run-chat.ts` reads its ports through one.
  const askRef = useRef<((report: ErrorReport) => void) | undefined>(undefined);
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
    askAboutError: report => askRef.current?.(report),
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
  askRef.current = sessions.askAboutError;
  if (
    page.state === null ||
    page.model === null ||
    page.whole === null ||
    !snapshot.matches('active')
  ) {
    return (
      <Page>
        <Card>
          {(snapshot.context.error ?? page.projectionError) ? (
            <EmptyState
              title={snapshot.context.error ?? page.projectionError ?? ''}
              action={<ErrorActions copy={snapshot.context.error ?? page.projectionError ?? ''} />}
            />
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
      dispatch={action => {
        if (action.type === 'run-again') props.navigation.runAgain();
        else if (action.type === 'open-window') props.navigation.openWindow();
        else page.dispatch(action);
      }}
      chat={sessions}
      toolbar={intents.controls}
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
  readonly host: RunPageSeed['identity']['host'];
  readonly schedule: ScheduleTimeout;
}): {readonly fanout: RecordsFanout; readonly ports: GatewayRunPorts} {
  const {bridge, host, schedule} = input;
  const fanout = recordsFanout();
  const reader = gatewayReader(
    gatewayTransport(bridge, host),
    gatewayContentCache(indexedDB),
    schedule,
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
