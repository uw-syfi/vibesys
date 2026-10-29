import {clockFrom} from '@vibesys/machine';
import {createActor, fromCallback, fromPromise} from 'xstate';
import {type ThreadsEvent, threadsMachine} from '../../state/index.js';
import {sessionsActor} from './actor.js';
import {type ChatServicePorts, chatService} from './service.js';

export function threadsActor(ports: ChatServicePorts) {
  const service = chatService(ports);
  return createActor(
    threadsMachine.provide({
      actors: {
        list: fromPromise(({signal}) => service.list(signal)),
        watch: fromCallback<
          ThreadsEvent,
          {id: string; history: import('../../state/index.js').ChatHistory}
        >(({input, sendBack}) => {
          const actor = sessionsActor({
            ...ports,
            sessionId: input.id,
            enabled: true,
            history: input.history,
          });
          const subscription = actor.subscribe(snapshot =>
            sendBack({
              type: 'history',
              id: input.id,
              history: snapshot.context.history,
              error: snapshot.context.error,
            }),
          );
          actor.start();
          actor.send({type: 'toggle'});
          return () => {
            subscription.unsubscribe();
            actor.stop();
          };
        }),
      },
    }),
    {clock: clockFrom(ports.schedule)},
  );
}
