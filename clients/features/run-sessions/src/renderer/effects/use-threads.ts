import type {ChatSessionCreated} from '@vibesys/runs/chat';
import {useCallback, useEffect, useRef, useState} from 'react';
import type {SnapshotFrom} from 'xstate';
import type {threadsMachine} from '../../state/index.js';
import type {ChatServicePorts} from './service.js';
import {threadsActor} from './threads.js';

export function useChatThreads(ports: ChatServicePorts, enabled: boolean) {
  const current = useRef<ReturnType<typeof threadsActor> | null>(null);
  const [snapshot, setSnapshot] = useState<SnapshotFrom<typeof threadsMachine> | null>(null);
  useEffect(() => {
    if (!enabled) return;
    const actor = threadsActor(ports);
    current.current = actor;
    const subscription = actor.subscribe(setSnapshot);
    actor.start();
    return () => {
      current.current = null;
      subscription.unsubscribe();
      actor.stop();
    };
  }, [ports, enabled]);
  const select = useCallback(
    (id: string | null) => current.current?.send({type: 'select', id}),
    [],
  );
  const refresh = useCallback(() => current.current?.send({type: 'refresh'}), []);
  const created = useCallback(
    (session: ChatSessionCreated) => current.current?.send({type: 'created', session}),
    [],
  );
  const reload = useCallback(() => {
    current.current?.send({type: 'refresh'});
    current.current?.send({type: 'reload'});
  }, []);
  return {context: snapshot?.context, select, refresh, created, reload};
}
