import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge} from '@vibesys/ipc';
import type {ScheduleTimeout} from '@vibesys/machine';
import {
  type RelaunchObservation,
  relaunchLabel,
  relaunchRequest,
  relaunchTarget,
} from '@vibesys/runs/intents';
import {useEffect, useRef, useState} from 'react';
import type {SnapshotFrom} from 'xstate';
import type {relaunchMachine} from '../state/index.js';
import {relaunchActor} from './effects/relaunch.js';
import type {RelaunchView} from './ui/relaunch.js';

/** Keep the in-flight request and result while the reader observes its new epoch. */
export function useRelaunch(props: {
  readonly bridge: GatewayBridge;
  readonly host: HostKey;
  readonly projectRoot: string;
  readonly runId: string;
  readonly observation: RelaunchObservation | undefined;
  readonly schedule: ScheduleTimeout;
}): RelaunchView | undefined {
  const [snapshot, setSnapshot] = useState<SnapshotFrom<typeof relaunchMachine> | null>(null);
  const current = useRef<ReturnType<typeof relaunchActor> | null>(null);
  const observation = useRef(props.observation);
  observation.current = props.observation;
  const enabled = props.observation !== undefined;
  useEffect(() => {
    const initial = observation.current;
    if (!initial) return;
    const actor = relaunchActor({
      bridge: props.bridge,
      host: props.host,
      request: relaunchRequest(props.projectRoot, props.runId, initial.epoch),
      observation: initial,
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
  }, [props.bridge, props.host, props.projectRoot, props.runId, props.schedule]);
  useEffect(() => {
    if (props.observation) current.current?.send({type: 'observe', observation: props.observation});
  }, [props.observation]);
  if (!enabled || !snapshot) return undefined;
  if (
    snapshot.matches('ready') &&
    relaunchTarget(snapshot.context.observation, snapshot.context.liveness) === null &&
    snapshot.context.result === null
  )
    return undefined;
  return {
    state:
      snapshot.matches('ready') &&
      relaunchTarget(snapshot.context.observation, snapshot.context.liveness) === null
        ? 'unavailable'
        : snapshot.value,
    result: relaunchLabel(snapshot.context.result),
    launch: () => current.current?.send({type: 'relaunch'}),
  };
}
