import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge} from '@vibesys/ipc';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {
  confirmedRelaunchEpoch,
  type RunWriterFacts,
  relaunchLabel,
  relaunchRequest,
  relaunchTarget,
} from '@vibesys/runs/intents';
import {useEffect, useRef, useState} from 'react';
import {createActor, type SnapshotFrom} from 'xstate';
import {type relaunchMachine, writerRecoveryMachine} from '../state/index.js';
import {relaunchActor} from './effects/relaunch.js';
import type {RelaunchView} from './ui/relaunch.js';

/** Keep the in-flight request and result while the reader observes its new epoch. */
export function useRelaunch(props: {
  readonly bridge: GatewayBridge;
  readonly host: HostKey;
  readonly projectRoot: string;
  readonly runId: string;
  readonly writer: RunWriterFacts;
  readonly enabled: boolean;
  readonly historyEpoch: bigint;
  readonly schedule: ScheduleTimeout;
}): RelaunchView | undefined {
  const [snapshot, setSnapshot] = useState<SnapshotFrom<typeof relaunchMachine> | null>(null);
  const current = useRef<ReturnType<typeof relaunchActor> | null>(null);
  const writer = useRef(props.writer);
  writer.current = props.writer;
  const enabled = props.enabled;
  useEffect(() => {
    if (!enabled) return;
    const initial = writer.current.observation ?? {epoch: 0n};
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
    actor.send({type: 'writer', facts: writer.current});
    return () => {
      current.current = null;
      subscription.unsubscribe();
      actor.stop();
    };
  }, [props.bridge, props.host, props.projectRoot, props.runId, props.schedule, enabled]);
  useEffect(() => {
    current.current?.send({type: 'writer', facts: props.writer});
  }, [props.writer]);
  useEffect(() => {
    current.current?.send({type: 'epoch', epoch: props.historyEpoch});
  }, [props.historyEpoch]);
  if (!enabled || !snapshot) return undefined;
  const confirmed = snapshot.context.attempted
    ? confirmedRelaunchEpoch(
        snapshot.context.request,
        snapshot.context.observation,
        props.writer.liveness,
        snapshot.context.historyEpoch,
      )
    : null;
  const appliedEpoch =
    confirmed ??
    (snapshot.context.result?.outcome.case === 'epoch'
      ? snapshot.context.result.outcome.value
      : null);
  return {
    state: snapshot.value,
    result:
      appliedEpoch === null
        ? relaunchLabel(snapshot.context.result)
        : `Epoch ${appliedEpoch.toString()}`,
    appliedEpoch,
    available:
      props.writer.observation !== undefined &&
      relaunchTarget(props.writer.observation, props.writer.liveness) !== null,
    launch: () => {
      current.current?.send({type: 'writer', facts: writer.current});
      current.current?.send({type: 'relaunch'});
    },
  };
}

/** The page owns the lifecycle reader; this hook only translates the machine's refresh request. */
export function useWriterRecovery(
  schedule: ScheduleTimeout,
  waiting: boolean,
  refresh: (() => void) | undefined,
) {
  const latest = useRef({refresh, waiting});
  latest.current = {refresh, waiting};
  const current = useRef<ReturnType<typeof createActor<typeof writerRecoveryMachine>> | null>(null);
  useEffect(() => {
    const actor = createActor(
      writerRecoveryMachine.provide({
        actions: {
          refresh: () => latest.current.refresh?.(),
        },
      }),
      {clock: clockFrom(schedule)},
    ).start();
    current.current = actor;
    actor.send({type: 'observe', waiting: latest.current.waiting});
    return () => {
      current.current = null;
      actor.stop();
    };
  }, [schedule]);
  useEffect(() => {
    current.current?.send({type: 'observe', waiting});
  }, [waiting]);
}
