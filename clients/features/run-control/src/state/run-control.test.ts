import {describe, expect, test} from 'bun:test';
import type {WelcomeResult} from '@vibesys/ipc';
import {randomChrome} from '@vibesys/ipc/testing';
import {checkMachine} from '@vibesys/machine/testing';
import {exactResumeRunId} from '@vibesys/runs';
import {createActor, fromPromise} from 'xstate';
import {
  type RunCommand,
  runControlMachine,
  type WelcomeAction,
  type WelcomeState,
  welcomeView,
} from './run-control.js';

function failed<T>(error: string): WelcomeResult<T> {
  return {ok: false, error, authNeeded: false};
}

/** The kinds `progress()` has no text for. */
const TEXTLESS_KINDS: ReadonlySet<RunCommand['kind']> = new Set([
  'stop',
  'retry',
  'show-run',
  'show-welcome',
]);

const COMMANDS: readonly RunCommand[] = [
  {kind: 'attach', host: 'local', instance: '0123456789ab'},
  {kind: 'resume', host: 'local', project: '/p', run: exactResumeRunId('run-1')},
  {kind: 'stop', host: 'local', instance: '0123456789ab', force: false},
  {kind: 'retry'},
  {kind: 'show-run'},
  {kind: 'show-welcome'},
];

type Banner = WelcomeState['banner'];

/** A textless command retires superseded progress but keeps an asynchronous problem. */
function assertTextlessCommandBanner(prior: Banner, command: RunCommand, banner: Banner): void {
  if (!TEXTLESS_KINDS.has(command.kind)) return;
  if (prior?.source === 'progress') expect(banner).toBeNull();
  if (prior?.source === 'problem') expect(banner).toEqual(prior);
}

/** A failure replaces the banner; a success clears command-owned text but keeps a problem. */
function assertSettledBanner(prior: Banner, output: WelcomeResult<null>, banner: Banner): void {
  const kept = prior?.source === 'problem' ? prior : null;
  expect(banner).toEqual(output.ok ? kept : {text: output.error, error: true, source: 'reply'});
}

function assertBanner(prior: Banner, event: {readonly type: string}, banner: Banner): void {
  if (event.type === 'problem')
    expect(banner).toEqual({text: 'settings unreadable', error: true, source: 'problem'});
  if (event.type === 'command')
    assertTextlessCommandBanner(
      prior,
      (event as Extract<WelcomeAction, {type: 'command'}>).command,
      banner,
    );
  if (event.type === 'xstate.done.actor.command')
    assertSettledBanner(prior, (event as unknown as {output: WelcomeResult<null>}).output, banner);
}

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
      also: (before, event, after) =>
        assertBanner(welcomeView(before).banner, event, welcomeView(after).banner),
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
      source: 'progress',
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

  test('regression: a textless stop command clears the progress text of an outstanding attach', () => {
    const actor = createActor(
      runControlMachine.provide({
        actors: {
          command: fromPromise<WelcomeResult<null>, RunCommand>(() => new Promise(() => {})),
        },
      }),
    ).start();
    actor.send({type: 'command', command: {kind: 'attach', host: 'local', instance: 'run-a'}});
    expect(welcomeView(actor.getSnapshot()).banner).toEqual({
      text: 'Attaching to run-a…',
      error: false,
      source: 'progress',
    });

    // `stop` has no progress text of its own, but it supersedes the attach, so the banner must not
    // keep describing the abandoned attach.
    actor.send({
      type: 'command',
      command: {kind: 'stop', host: 'local', instance: 'run-b', force: false},
    });
    expect(welcomeView(actor.getSnapshot()).banner).toBeNull();
  });

  test('regression: a textless command and its successful reply retain an asynchronous problem', async () => {
    const replies: Array<(result: WelcomeResult<null>) => void> = [];
    const actor = createActor(
      runControlMachine.provide({
        actors: {
          command: fromPromise<WelcomeResult<null>, RunCommand>(
            () => new Promise(resolve => replies.push(resolve)),
          ),
        },
      }),
    ).start();
    actor.send({type: 'problem', text: 'Connection lost.'});
    actor.send({
      type: 'command',
      command: {kind: 'stop', host: 'local', instance: 'run-a', force: false},
    });
    const problem = {text: 'Connection lost.', error: true, source: 'problem'} as const;
    expect(welcomeView(actor.getSnapshot()).banner).toEqual(problem);

    expect(replies).toHaveLength(1);
    replies[0]?.({ok: true, value: null});
    await flush();
    expect(welcomeView(actor.getSnapshot())).toMatchObject({banner: problem, busy: false});
  });
});
