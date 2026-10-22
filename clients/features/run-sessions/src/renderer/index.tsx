import {newRequestId} from '@vibesys/request-identity';
import {type ChatSteerProposed, chatRequest} from '@vibesys/runs/chat';
import {useEffect, useRef, useState} from 'react';
import type {SnapshotFrom} from 'xstate';
import type {runSessionsMachine} from '../state/index.js';
import {type SessionPorts, sessionsActor} from './effects/actor.js';
import {SessionPane} from './ui/pane.js';

/** Mount beside the Run screen, keyed by host/run, with the host's transport switch. */
export function useRunSessions(
  props: SessionPorts & {
    readonly projectRoot: string;
    readonly steerBusy: boolean;
    readonly sendSteer: (proposal: ChatSteerProposed) => void;
  },
) {
  const current = useRef<ReturnType<typeof sessionsActor> | null>(null);
  const [snapshot, setSnapshot] = useState<SnapshotFrom<typeof runSessionsMachine> | null>(null);
  const [text, setText] = useState('');
  useEffect(() => {
    const actor = sessionsActor({
      bridge: props.bridge,
      host: props.host,
      runId: props.runId,
      sessionId: props.sessionId,
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
  }, [props.bridge, props.host, props.runId, props.sessionId, props.enabled, props.schedule]);
  const toggle = () => current.current?.send({type: 'toggle'});
  const open = snapshot?.matches('active') ?? false;
  if (!props.enabled || !snapshot) return {open: false, toggle, panel: null};
  const panel = (
    <SessionPane
      close={toggle}
      history={snapshot.context.history}
      text={text}
      edit={setText}
      busy={snapshot.matches({active: {sending: 'posting'}})}
      error={snapshot.context.error}
      contentFailed={snapshot.context.contentFailed}
      reload={() => current.current?.send({type: 'reload'})}
      uncertain={snapshot.context.uncertain}
      steerBusy={props.steerBusy}
      steer={props.sendSteer}
      retry={() => current.current?.send({type: 'retry'})}
      send={() => {
        current.current?.send({
          type: 'send',
          request: chatRequest({
            projectRoot: props.projectRoot,
            runId: props.runId,
            sessionId: props.sessionId,
            requestId: newRequestId(),
            text,
          }),
        });
        setText('');
      }}
    />
  );
  return {open, toggle, panel};
}
export {sessionsActor} from './effects/actor.js';
