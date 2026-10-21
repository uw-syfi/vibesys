import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge} from '@vibesys/ipc';
import type {ScheduleTimeout} from '@vibesys/machine';
import {type RelaunchTarget, runIntent} from '@vibesys/runs/intents';
import {Stack} from '@vibesys/ui';
import {type JSX, useEffect, useRef, useState} from 'react';
import type {SnapshotFrom} from 'xstate';
import type {runIntentsMachine} from '../state/index.js';
import {intentsActor} from './effects/controls.js';
import {Relaunch} from './relaunch.js';
import {IntentControls} from './ui/controls.js';

/** Mount with a key of host/runId. enabled is the host's runTransportV2 setting. */
export function RunIntents(props: {
  readonly bridge: GatewayBridge;
  readonly host: HostKey;
  readonly projectRoot: string;
  readonly runId: string;
  readonly enabled: boolean;
  readonly schedule: ScheduleTimeout;
  readonly relaunch?: RelaunchTarget;
}): JSX.Element | null {
  const [snapshot, setSnapshot] = useState<SnapshotFrom<typeof runIntentsMachine> | null>(null);
  const [text, setText] = useState('');
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
  if (!props.enabled || !snapshot) return null;
  const send = (action: 'pause' | 'resume' | 'stop' | 'steer') => {
    const request = runIntent(
      {projectRoot: props.projectRoot, runId: props.runId, requestId: crypto.randomUUID()},
      action,
      text,
    );
    current.current?.send({type: 'send', request});
  };
  return (
    <Stack gap="space-2">
      <IntentControls
        busy={snapshot.matches({active: 'sending'})}
        entries={snapshot.context.entries}
        error={snapshot.context.error}
        warning={snapshot.context.warning}
        text={text}
        edit={setText}
        send={send}
        retry={requestId => current.current?.send({type: 'retry', requestId})}
      />
      {props.relaunch ? (
        <Relaunch {...props} target={props.relaunch} key={props.relaunch.epoch.toString()} />
      ) : null}
    </Stack>
  );
}

export {intentsActor} from './effects/controls.js';
export {relaunchActor} from './effects/relaunch.js';
export {Relaunch} from './relaunch.js';
