import type {HostKey, RecordedProjectRoot} from '@vibesys/hosts';
import type {GatewayBridge} from '@vibesys/ipc';
import type {ScheduleTimeout} from '@vibesys/machine';
import {newRequestId} from '@vibesys/request-identity';
import {
  type RelaunchObservation,
  type RunIntentAction,
  runIntent,
  runReference,
} from '@vibesys/runs/intents';
import {type JSX, useEffect, useRef, useState} from 'react';
import type {SnapshotFrom} from 'xstate';
import type {runIntentsMachine} from '../state/index.js';
import {intentsActor} from './effects/controls.js';
import {useRelaunch} from './relaunch.js';
import {IntentControls} from './ui/controls.js';

/** Mount with a key of host/runId. enabled is the host's runTransportV2 setting. */
export function useRunIntents(props: {
  readonly bridge: GatewayBridge;
  readonly host: HostKey;
  readonly projectRoot: RecordedProjectRoot | undefined;
  readonly runId: string;
  readonly enabled: boolean;
  readonly schedule: ScheduleTimeout;
  readonly relaunch?: RelaunchObservation;
}) {
  const [snapshot, setSnapshot] = useState<SnapshotFrom<typeof runIntentsMachine> | null>(null);
  const [text, setText] = useState('');
  const [guidanceOpen, showGuidance] = useState(false);
  const current = useRef<ReturnType<typeof intentsActor> | null>(null);
  useEffect(() => {
    const actor = intentsActor({
      bridge: props.bridge,
      host: props.host,
      runId: props.runId,
      enabled: props.enabled,
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
  }, [props.bridge, props.host, props.runId, props.enabled, props.schedule]);
  const projectRoot = props.projectRoot;
  const relaunch = useRelaunch({
    bridge: props.bridge,
    host: props.host,
    projectRoot: projectRoot ?? '',
    runId: props.runId,
    observation: props.enabled && projectRoot !== undefined ? props.relaunch : undefined,
    schedule: props.schedule,
  });
  const busy = snapshot?.matches({active: 'sending'}) ?? false;
  const sendRequest = (
    request: Parameters<NonNullable<typeof current.current>['send']>[0] & {type: 'send'},
  ) => current.current?.send(request);
  if (!props.enabled || !snapshot || projectRoot === undefined)
    return {controls: null, busy, sendRequest};
  const send = (action: RunIntentAction) => {
    const request = runIntent(
      {
        ...runReference(projectRoot, props.runId),
        requestId: newRequestId(),
        expectedEpoch: props.relaunch?.epoch ?? 0n,
      },
      action,
      text,
    );
    current.current?.send({type: 'send', request});
    if (action === 'steer') showGuidance(false);
  };
  const controls = (
    <IntentControls
      {...(relaunch ? {relaunch} : {})}
      busy={snapshot.matches({active: 'sending'})}
      entries={snapshot.context.entries}
      error={snapshot.context.error}
      warning={snapshot.context.warning}
      guidanceOpen={guidanceOpen}
      showGuidance={showGuidance}
      text={text}
      edit={setText}
      send={send}
      retry={requestId => current.current?.send({type: 'retry', requestId})}
    />
  );
  return {controls, busy, sendRequest};
}

export function RunIntents(props: Parameters<typeof useRunIntents>[0]): JSX.Element | null {
  return useRunIntents(props).controls;
}

export {intentsActor} from './effects/controls.js';
export {relaunchActor} from './effects/relaunch.js';
