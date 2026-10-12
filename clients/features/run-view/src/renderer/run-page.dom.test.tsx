/** Public rendered-run interactions in a DOM: viewport ownership, round navigation, and the live
 * command capability. The Fakes model only the injected command port and deferred replies. */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {event} from '@vibesys/contracts/testing';
import {type RunCommand, type RunCommandOutcome, runStep} from '@vibesys/run-session';
import {type CoreState, initialCoreState} from '@vibesys/run-state';
import {Window} from 'happy-dom';
import {act} from 'react';
import type {Root} from 'react-dom/client';
import {RunPage} from './index.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));

beforeAll(() => {
  for (const name of installed) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  }
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});

afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT']) {
    Reflect.deleteProperty(globalThis, name);
  }
  await window.happyDOM.close();
});

interface MountedRun {
  readonly root: Root;
  readonly container: HTMLElement;
  readonly render: (run: CoreState) => void;
}

async function mountRun(
  initial: CoreState,
  command: ((input: RunCommand) => Promise<RunCommandOutcome>) | null = null,
): Promise<MountedRun> {
  const {createRoot} = await import('react-dom/client');
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const render = (run: CoreState): void => {
    root.render(
      <RunPage
        run={run}
        session={
          command === null
            ? null
            : {
                hasSnapshot: true,
                status: 'connected',
                error: null,
                controls: {status: 'connected'},
              }
        }
        replayFailure={null}
        onReattach={() => undefined}
        onReconnectControls={() => undefined}
        onRetryReplay={() => undefined}
        onCommand={command}
      />,
    );
  };
  await act(async () => render(initial));
  return {root, container, render};
}

async function unmount({root, container}: MountedRun): Promise<void> {
  await act(async () => root.unmount());
  container.remove();
}

function outputRun(length: number, status: 'running' | 'paused' = 'running'): CoreState {
  const state = runStep(initialCoreState(), {
    type: 'events',
    events: Array.from({length}, (_, index) =>
      event(index + 1, 'agent_output_chunk', `activity ${index + 1}`),
    ),
  }).state;
  if (status === 'running') return state;
  return runStep(state, {
    type: 'events',
    events: [
      event(length + 1, 'run_status_changed', {
        data: {kind: 'run_status_changed', status: 'paused', previous: 'running'},
      }),
    ],
  }).state;
}

test('starts at the tail, follows only while pinned, and preserves a historical viewport', async () => {
  const mounted = await mountRun(outputRun(80));
  const viewport = mounted.container.querySelector<HTMLElement>('[aria-label^="Transcript,"]');
  if (viewport === null) throw new Error('transcript viewport missing');
  expect(viewport.tabIndex).toBe(0);
  expect(viewport.scrollTop).toBe(80 * 144 - 576);
  expect(mounted.container.textContent).toContain('activity 80');
  expect(mounted.container.textContent).not.toContain('activity 1');
  const finalRow = mounted.container.querySelector('[aria-posinset="80"]');
  expect(finalRow?.getAttribute('aria-setsize')).toBe('80');

  await act(async () => {
    viewport.scrollTop = 1440;
    viewport.dispatchEvent(new window.Event('scroll') as unknown as Event);
  });
  await act(async () => mounted.render(outputRun(81)));
  expect(viewport.scrollTop).toBe(1440);
  expect(mounted.container.textContent).not.toContain('activity 81');

  await act(async () => {
    viewport.scrollTop = 81 * 144 - 576;
    viewport.dispatchEvent(new window.Event('scroll') as unknown as Event);
  });
  await act(async () => mounted.render(outputRun(82)));
  expect(viewport.scrollTop).toBe(82 * 144 - 576);
  expect(mounted.container.textContent).toContain('activity 82');

  viewport.focus();
  expect(document.activeElement).toBe(viewport);
  await unmount(mounted);
});

test('round rail buttons are keyboard-focusable navigation into the transcript', async () => {
  const run = runStep(initialCoreState(), {
    type: 'events',
    events: [
      event(1, 'agent_output_chunk', {
        round_label: 'round-1-implementer',
        data: {kind: 'agent_output_chunk', channel: 'assistant', content: 'first round'},
      }),
      event(2, 'agent_output_chunk', {
        round_label: 'round-2-implementer',
        data: {kind: 'agent_output_chunk', channel: 'assistant', content: 'second round'},
      }),
    ],
  }).state;
  const mounted = await mountRun(run);
  const roundOne = [...mounted.container.querySelectorAll<HTMLButtonElement>('.round-link')].find(
    button => button.textContent?.includes('Round 1') === true,
  );
  if (roundOne === undefined) throw new Error('round-one navigation missing');
  roundOne.focus();
  expect(document.activeElement).toBe(roundOne);
  await act(async () => roundOne.click());
  expect(roundOne.getAttribute('aria-pressed')).toBe('true');
  expect(mounted.container.textContent).toContain('first round');
  expect(mounted.container.textContent).not.toContain('second round');
  await unmount(mounted);
});

test('clipped tool arguments and output are named keyboard reading regions', async () => {
  const run = runStep(initialCoreState(), {
    type: 'events',
    events: [
      event(1, 'tool_call', {
        invocation_id: 'turn-1',
        data: {
          kind: 'tool_call',
          tool: 'read_file',
          call_id: 'call-1',
          args: {path: `src/${'nested/'.repeat(30)}main.py`},
        },
      }),
      event(2, 'tool_result', {
        invocation_id: 'turn-1',
        data: {
          kind: 'tool_result',
          tool: 'read_file',
          call_id: 'call-1',
          content: Array.from({length: 40}, (_, index) => `line ${index + 1}`).join('\n'),
        },
      }),
    ],
  }).state;
  const mounted = await mountRun(run);
  const argumentsRegion = mounted.container.querySelector<HTMLElement>(
    'section[aria-label="read_file tool arguments"]',
  );
  const outputRegion = mounted.container.querySelector<HTMLElement>(
    'section[aria-label="read_file output"]',
  );
  if (argumentsRegion === null || outputRegion === null) {
    throw new Error('tool reading regions missing');
  }

  expect(argumentsRegion.tabIndex).toBe(0);
  expect(outputRegion.tabIndex).toBe(0);
  argumentsRegion.focus();
  expect(document.activeElement).toBe(argumentsRegion);
  outputRegion.focus();
  expect(document.activeElement).toBe(outputRegion);
  expect(outputRegion.textContent).toContain('line 40');
  await unmount(mounted);
});

test('distinguishes pending tool calls from completed empty results', async () => {
  const run = runStep(initialCoreState(), {
    type: 'events',
    events: [
      event(1, 'tool_call', {
        invocation_id: 'turn-1',
        data: {
          kind: 'tool_call',
          tool: 'read_file',
          call_id: 'call-1',
          args: {path: 'src/main.py'},
        },
      }),
      event(2, 'tool_call', {
        invocation_id: 'turn-2',
        data: {
          kind: 'tool_call',
          tool: 'read_file',
          call_id: 'call-2',
          args: {path: 'src/empty.py'},
        },
      }),
      event(3, 'tool_result', {
        invocation_id: 'turn-2',
        data: {
          kind: 'tool_result',
          tool: 'read_file',
          call_id: 'call-2',
          content: '',
        },
      }),
    ],
  }).state;
  const mounted = await mountRun(run);
  const rows = mounted.container.querySelectorAll<HTMLElement>('.transcript-row');

  expect(rows).toHaveLength(2);
  expect(rows[0]?.textContent).toContain('Waiting for tool output…');
  expect(rows[1]?.textContent).toContain('No tool output was returned.');
  expect(rows[1]?.textContent).not.toContain('Waiting for tool output…');
  await unmount(mounted);
});

interface DeferredReply {
  readonly promise: Promise<RunCommandOutcome>;
  readonly resolve: (response: RunCommandOutcome) => void;
  readonly reject: (error: Error) => void;
}

class FakeCommands {
  readonly requests: RunCommand[] = [];
  readonly replies: DeferredReply[] = [];

  readonly send = (input: RunCommand): Promise<RunCommandOutcome> => {
    this.requests.push(input);
    let resolve!: (response: RunCommandOutcome) => void;
    let reject!: (error: Error) => void;
    const promise = new Promise<RunCommandOutcome>((onResolve, onReject) => {
      resolve = onResolve;
      reject = onReject;
    });
    this.replies.push({promise, resolve, reject});
    return promise;
  };
}

function button(container: HTMLElement, label: string): HTMLButtonElement {
  const found = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
    item => item.textContent === label,
  );
  if (found === undefined) throw new Error(`${label} button missing`);
  return found;
}

function ack(action: 'pause' | 'resume' | 'steer' | 'stop'): RunCommandOutcome {
  return {kind: 'acknowledged', action, status: 'consumed'};
}

async function type(input: HTMLInputElement, text: string): Promise<void> {
  const setValue = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
  if (setValue === undefined) throw new Error('happy-dom inputs have no value setter');
  await act(async () => {
    setValue.call(input, text);
    input.dispatchEvent(new window.Event('input', {bubbles: true}) as unknown as Event);
  });
}

test('live controls serialize pause, resume, steer, and stop through acknowledgments', async () => {
  const fake = new FakeCommands();
  const mounted = await mountRun(outputRun(1), fake.send);

  await act(async () => button(mounted.container, 'Pause').click());
  expect(fake.requests).toEqual([{type: 'command.pause'}]);
  expect(button(mounted.container, 'Pausing…').disabled).toBe(true);
  expect(button(mounted.container, 'Stop').disabled).toBe(true);
  await act(async () => fake.replies[0]?.resolve(ack('pause')));
  expect(mounted.container.textContent).toContain('pause consumed');

  await act(async () => mounted.render(outputRun(1, 'paused')));
  await act(async () => button(mounted.container, 'Resume').click());
  expect(fake.requests.at(-1)).toEqual({type: 'command.resume'});
  expect(button(mounted.container, 'Resuming…').disabled).toBe(true);
  await act(async () => fake.replies[1]?.resolve(ack('resume')));
  expect(mounted.container.textContent).toContain('resume consumed');

  const input = mounted.container.querySelector<HTMLInputElement>('#run-steer');
  if (input === null) throw new Error('steer input missing');
  await type(input, '  inspect the queue  ');
  const form = input.closest('form');
  if (form === null) throw new Error('steer form missing');
  await act(async () =>
    form.dispatchEvent(
      new window.Event('submit', {bubbles: true, cancelable: true}) as unknown as Event,
    ),
  );
  expect(fake.requests.at(-1)).toEqual({type: 'command.steer', text: 'inspect the queue'});
  expect(button(mounted.container, 'Sending…').disabled).toBe(true);
  await act(async () => fake.replies[2]?.resolve(ack('steer')));
  expect(input.value).toBe('');

  await act(async () => button(mounted.container, 'Stop').click());
  expect(fake.requests.at(-1)).toEqual({type: 'command.stop'});
  expect(button(mounted.container, 'Stopping…').disabled).toBe(true);
  await act(async () => fake.replies[3]?.resolve(ack('stop')));
  expect(mounted.container.textContent).toContain('stop consumed');
  await unmount(mounted);
});

test('live controls surface rejection while replay controls send no traffic', async () => {
  const fake = new FakeCommands();
  const mounted = await mountRun(outputRun(1), fake.send);
  await act(async () => button(mounted.container, 'Pause').click());
  await act(async () => fake.replies[0]?.resolve({kind: 'rejected', message: 'run is read only'}));
  expect(mounted.container.querySelector('[role="status"]')?.textContent).toBe('run is read only');
  expect(button(mounted.container, 'Pause').disabled).toBe(false);
  await unmount(mounted);

  const replay = await mountRun(outputRun(1));
  expect(button(replay.container, 'Pause').disabled).toBe(true);
  expect(button(replay.container, 'Stop').disabled).toBe(true);
  expect(replay.container.querySelector<HTMLInputElement>('#run-steer')?.disabled).toBe(true);
  expect(replay.container.textContent).toContain('Controls are unavailable while replaying');
  await unmount(replay);
});
