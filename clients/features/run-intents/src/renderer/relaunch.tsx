import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge} from '@vibesys/ipc';
import type {ScheduleTimeout} from '@vibesys/machine';
import {type RelaunchTarget, relaunchLabel, relaunchRequest} from '@vibesys/runs/intents';
import {type JSX, useEffect, useRef, useState} from 'react';
import type {SnapshotFrom} from 'xstate';
import type {relaunchMachine} from '../state/index.js';
import {relaunchActor} from './effects/relaunch.js';
import {RelaunchControl} from './ui/relaunch.js';

/** Key by host, run and observed epoch so stale replies cannot cross a run switch. */
export function Relaunch(props: {
  readonly bridge: GatewayBridge;
  readonly host: HostKey;
  readonly projectRoot: string;
  readonly runId: string;
  readonly target: RelaunchTarget;
  readonly schedule: ScheduleTimeout;
}): JSX.Element | null {
  const [snapshot, setSnapshot] = useState<SnapshotFrom<typeof relaunchMachine> | null>(null);
  const current = useRef<ReturnType<typeof relaunchActor> | null>(null);
  useEffect(() => {
    const actor = relaunchActor({
      bridge: props.bridge,
      host: props.host,
      request: relaunchRequest(props.projectRoot, props.runId, props.target.epoch),
      schedule: props.schedule,
    });
    current.current = actor;
    const subscription = actor.subscribe(setSnapshot);
    actor.start();
    return () => {
      current.current = null;
      subscription.unsubscribe();
      actor.stop();
    };
  }, [
    props.bridge,
    props.host,
    props.projectRoot,
    props.runId,
    props.target.epoch,
    props.schedule,
  ]);
  if (!snapshot) return null;
  return (
    <RelaunchControl
      state={snapshot.value}
      result={relaunchLabel(snapshot.context.result)}
      launch={() => current.current?.send({type: 'relaunch'})}
    />
  );
}
