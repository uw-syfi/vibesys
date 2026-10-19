/**
 * The chat panel end to end over fake ports: a proposal becomes a card, nothing is sent until Send,
 * the edited steer goes out as one control, and the server's delivery steps move the card.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {type CoreState, initialCoreState, reduceEvent} from '@vibesys/run-state';
import {Window} from 'happy-dom';
import {act, type JSX} from 'react';
import {createRoot} from 'react-dom/client';
import type {DeliverRequest} from '../../state/index.js';
import {ChatPanel} from '../ui/chat-panel.js';
import type {RunChatPorts} from './ports.js';
import {useRunChat} from './use-run-chat.js';

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

const CHAT = {agent_kind: 'chat', round_label: 'experiment-chat'} as const;

function fold(events: readonly RunEvent[]): CoreState {
  return events.reduce(reduceEvent, initialCoreState());
}

const THREAD = event(1, 'chat_thread_created', {
  ...CHAT,
  chat_thread_id: 't',
  data: {
    kind: 'chat_thread_created',
    thread_id: 't',
    title: 'Best idea',
    provider: 'claude',
    model: 'opus',
    created_at: '2026-01-01T00:00:00Z',
  },
});

const PROPOSAL = event(2, 'steer_proposed', {
  chat_thread_id: 't',
  data: {
    kind: 'steer_proposed',
    proposal_id: 'p-1',
    proposal: {text: 'keep bf16', target: {kind: 'role', id: 'implementer'}, mode: 'standing'},
  },
});

function delivery(sequence: number, step: Record<string, unknown>): RunEvent {
  return {
    ...event(sequence, 'run_status_changed'),
    type: 'steer_delivery',
    data: {kind: 'steer_delivery', delivery: step},
  } as unknown as RunEvent;
}

function fakePorts(sent: DeliverRequest[]): RunChatPorts {
  let next = 0;
  return {
    harnesses: () => Promise.resolve({kind: 'unsupported'}),
    createThread: () => Promise.resolve({ok: true, threadId: 'new'}),
    ask: () => Promise.resolve({ok: true}),
    control: request => {
      sent.push(request);
      return Promise.resolve({kind: 'acknowledged'});
    },
    newControlId: () => `control-${++next}`,
  };
}

function Harness(props: {core: CoreState; ports: RunChatPorts}): JSX.Element {
  const chat = useRunChat({identity: 'run', core: props.core, ports: props.ports});
  return (
    <div>
      <button type="button" id="chat-toggle" onClick={chat.toggle}>
        Chat
      </button>
      {chat.panel === null ? null : <ChatPanel {...chat.panel} />}
    </div>
  );
}

/**
 * Types into a field the way a browser does. Under happy-dom React reads a text field's change
 * from `keyup` on the focused field (it does not take the platform's `input` event as supported).
 */
function type(element: Element, value: string): void {
  const prototype = Object.getPrototypeOf(element) as object;
  const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
  (element as HTMLElement).focus();
  setter?.call(element, value);
  for (const kind of ['input', 'change', 'keyup']) {
    element.dispatchEvent(new window.Event(kind, {bubbles: true}) as unknown as Event);
  }
}

function press(container: Element, text: string): void {
  const found = [...container.querySelectorAll('button')].find(
    button => button.textContent?.trim() === text,
  );
  if (found === undefined) throw new Error(`no button ${text}`);
  found.click();
}

test('a proposed steer is edited, sent once as one control, and followed to delivery', async () => {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const sent: DeliverRequest[] = [];
  const ports = fakePorts(sent);
  const proposed = fold([THREAD, PROPOSAL]);
  await act(async () => root.render(<Harness core={proposed} ports={ports} />));
  expect(container.querySelector('textarea')).toBeNull();

  await act(async () => press(container, 'Chat'));
  const text = container.querySelector('textarea');
  expect(text?.value).toBe('keep bf16');
  // Nothing has been sent by showing the proposal.
  expect(sent).toEqual([]);

  await act(async () =>
    type(container.querySelector('textarea') as Element, 'keep bf16 and check the scale'),
  );
  await act(async () => press(container, 'Send'));
  expect(sent).toEqual([
    {
      control_id: 'control-1',
      action: 'steer',
      text: 'keep bf16 and check the scale',
      target: {kind: 'role', id: 'implementer'},
      mode: 'standing',
    },
  ]);
  expect(container.textContent).toContain('Sent');
  expect(container.querySelector('textarea')?.value).not.toBe('keep bf16 and check the scale');

  const reached = fold([
    THREAD,
    PROPOSAL,
    delivery(3, {state: 'queued', control_id: 'control-1', action: 'steer'}),
    delivery(4, {state: 'in_force', control_id: 'control-1', role: 'implementer'}),
  ]);
  await act(async () => root.render(<Harness core={reached} ports={ports} />));
  expect(container.textContent).toContain('In force');
  await act(async () => press(container, 'Withdraw'));
  expect(sent.at(-1)).toEqual({
    control_id: 'control-2',
    action: 'withdraw',
    withdraws: 'control-1',
  });
  expect(container.textContent).toContain('Withdrawing');
  await act(async () => root.unmount());
});

test('a proposal that is dismissed is gone and sends nothing', async () => {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const sent: DeliverRequest[] = [];
  await act(async () =>
    root.render(<Harness core={fold([THREAD, PROPOSAL])} ports={fakePorts(sent)} />),
  );
  await act(async () => press(container, 'Chat'));
  await act(async () => press(container, 'Dismiss'));
  expect(container.textContent).not.toContain('keep bf16');
  expect(sent).toEqual([]);
  await act(async () => root.unmount());
});
