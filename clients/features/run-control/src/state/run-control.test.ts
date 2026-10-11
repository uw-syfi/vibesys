import {describe, expect, test} from 'bun:test';
import type {WelcomeResult} from '@vibesys/ipc';
import {randomChrome} from '@vibesys/ipc/testing';
import {checkMachine} from '@vibesys/machine/testing';
import {exactResumeRunId} from '@vibesys/runs';
import {createActor, fromPromise} from 'xstate';
import {type RunCommand, runControlMachine, welcomeView} from './run-control.js';

function failed<T>(error: string): WelcomeResult<T> {
  return {ok: false, error, authNeeded: false};
}

const COMMANDS: readonly RunCommand[] = [
  {kind: 'attach', host: 'local', instance: '0123456789ab'},
  {kind: 'resume', host: 'local', project: '/p', run: exactResumeRunId('run-1')},
  {kind: 'stop', host: 'local', instance: '0123456789ab', force: false},
  {kind: 'retry'},
];

/** Let settled operations report: promise callbacks only, no timers. */
async function flush(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

describe('run-control machine', () => {
  test('model: busy exactly while a command runs, and a settled command sets the banner', () => {
    checkMachine({
      machine: runControlMachine,
      input: undefined,
      event: (random, live) => {
        const settle = live.operations.flatMap(operation => [
          live.done(operation, random.bool() ? {ok: true, value: null} : failed('failed')),
          live.error(operation, new Error('bridge failed')),
        ]);
        return random.pick([
          {type: 'command', command: random.pick(COMMANDS)},
          {type: 'chrome', chrome: randomChrome(random)},
          {type: 'problem', text: 'settings unreadable'},
          ...settle,
          ...settle,
        ]);
      },
      invariants: snapshot =>
        welcomeView(snapshot).busy === snapshot.matches('commanding')
          ? []
          : [`busy ${welcomeView(snapshot).busy} in ${String(snapshot.value)}`],
      also: (_before, event, after) => {
        const view = welcomeView(after);
        if (event.type === 'problem')
          expect(view.banner).toEqual({text: 'settings unreadable', error: true});
        if (event.type === 'xstate.done.actor.command') {
          const {output} = event as unknown as {output: WelcomeResult<null>};
          expect(view.banner).toEqual(output.ok ? null : {text: output.error, error: true});
        }
      },
    });
  });

  test('the banner answers the latest command only, whatever order the replies arrive in', async () => {
    const replies = new Map<string, (result: WelcomeResult<null>) => void>();
    const actor = createActor(
      runControlMachine.provide({
        actors: {
          command: fromPromise<WelcomeResult<null>, RunCommand>(
            ({input}) => new Promise(resolve => replies.set(input.kind, resolve)),
          ),
        },
      }),
    ).start();
    actor.send({type: 'command', command: COMMANDS[0] as RunCommand});
    actor.send({type: 'command', command: COMMANDS[1] as RunCommand});
    expect(welcomeView(actor.getSnapshot()).banner).toEqual({
      text: 'Resuming run-1…',
      error: false,
    });

    replies.get('resume')?.({ok: true, value: null});
    await flush();
    expect(welcomeView(actor.getSnapshot())).toEqual(
      expect.objectContaining({banner: null, busy: false}),
    );
    replies.get('attach')?.(failed('attach failed late'));
    await flush();
    expect(welcomeView(actor.getSnapshot())).toMatchObject({banner: null, busy: false});
  });
});
