import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge, WelcomeBridge} from '@vibesys/ipc';
import {type JSX, useEffect, useRef, useState} from 'react';
import {getInitialSnapshot} from 'xstate';
import {gatewayRecordsMachine} from '../state/index.js';
import {recordsActor} from './effects/records.js';
import {RecordsPanel} from './ui/records.js';

export function GatewayRecords(props: {
  readonly bridge: GatewayBridge;
  readonly settings: WelcomeBridge;
  readonly host: HostKey;
  readonly runId: string;
  readonly schedule: import('@vibesys/machine').ScheduleTimeout;
}): JSX.Element {
  const [snapshot, setSnapshot] = useState(() => getInitialSnapshot(gatewayRecordsMachine));
  const current = useRef<ReturnType<typeof recordsActor> | null>(null);
  useEffect(() => {
    const actor = recordsActor(
      props.bridge,
      props.settings,
      props.host,
      props.runId,
      props.schedule,
    );
    current.current = actor;
    const subscription = actor.subscribe(setSnapshot);
    actor.start();
    return () => {
      current.current = null;
      subscription.unsubscribe();
      actor.stop();
    };
  }, [props.bridge, props.settings, props.host, props.runId, props.schedule]);
  return (
    <RecordsPanel
      enabled={snapshot.context.enabled}
      rows={snapshot.context.rows}
      busy={snapshot.matches('checking') || snapshot.matches('saving')}
      error={snapshot.context.error}
      change={enabled => current.current?.send({type: 'toggle', enabled})}
    />
  );
}
