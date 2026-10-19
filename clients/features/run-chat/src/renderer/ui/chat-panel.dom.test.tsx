/**
 * The chat panel over a thread the fold holds: it settles and shows the answer, and a failure to
 * draw it stays in the panel. The layout is a fake one (a row's height follows its text), because
 * a thread keeps each row's measured height and a row that is measured differently every pass is
 * what made the whole window fail.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {type CoreState, initialCoreState, reduceEvent} from '@vibesys/run-state';
import {Window} from 'happy-dom';
import {act, type JSX} from 'react';
import {createRoot} from 'react-dom/client';
import {type ChatPanelView, panelInitial} from '../../state/index.js';
import type {RunChatPorts} from '../effects/ports.js';
import {useRunChat} from '../effects/use-run-chat.js';
import {ChatPanel} from './chat-panel.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));

const LINE = 24;
const WIDTH = 30;

beforeAll(() => {
  for (const name of installed) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  }
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
  Object.defineProperty(window.HTMLElement.prototype, 'offsetHeight', {
    configurable: true,
    get(this: HTMLElement) {
      const text = this.textContent ?? '';
      return text === '' ? 0 : LINE * Math.ceil(text.length / WIDTH);
    },
  });
});

afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT']) {
    Reflect.deleteProperty(globalThis, name);
  }
  await window.happyDOM.close();
});

const CHAT = {agent_kind: 'chat', round_label: 'experiment-chat', chat_thread_id: 't'} as const;
const QUESTION =
  'What is the best measured result of this run so far? Read-only question, change nothing.';
const ANSWER = 'The best idea is the lock-free ring.';

function fold(events: readonly RunEvent[]): CoreState {
  return events.reduce(reduceEvent, initialCoreState());
}

const THREAD = event(1, 'chat_thread_created', {
  ...CHAT,
  data: {
    kind: 'chat_thread_created',
    thread_id: 't',
    title: 'Best idea',
    provider: 'claude',
    model: 'opus',
    created_at: '2026-01-01T00:00:00Z',
  },
});

/** The terminal event alone: the agent streamed nothing before it. */
const ANSWERED = event(2, 'chat', {
  ...CHAT,
  text: QUESTION,
  data: {kind: 'chat', answer: ANSWER, invocation_id: 'turn-1'},
});

function viewOf(items: ChatPanelView['items']): ChatPanelView {
  return {
    panel: panelInitial,
    busy: false,
    threads: [],
    shown: 't',
    items,
    roles: [],
    readOnly: false,
    dispatch: () => undefined,
  };
}

async function mount(node: JSX.Element) {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(node));
  return {container, unmount: () => act(async () => root.unmount())};
}

const PORTS: RunChatPorts = {
  harnesses: () => Promise.resolve({kind: 'unsupported'}),
  createThread: () => Promise.resolve({ok: true, threadId: 'new'}),
  ask: () => Promise.resolve({ok: true}),
  control: () => Promise.resolve({kind: 'acknowledged'}),
  newControlId: () => 'control-1',
};

function Harness(props: {readonly core: CoreState}): JSX.Element {
  const chat = useRunChat({identity: 'run', core: props.core, ports: PORTS});
  return (
    <div>
      <button type="button" onClick={chat.toggle}>
        Chat
      </button>
      {chat.panel === null ? null : <ChatPanel {...chat.panel} />}
    </div>
  );
}

test('a thread whose answer was not streamed settles and shows the question and the answer', async () => {
  const {container, unmount} = await mount(<Harness core={fold([THREAD, ANSWERED])} />);
  await act(async () => container.querySelector('button')?.click());
  expect(container.textContent).toContain(QUESTION);
  expect(container.textContent).toContain(ANSWER);
  expect(container.textContent).not.toContain('Chat failed');
  await unmount();
});

test('a panel that cannot be drawn shows an error state with Close, and sends Close', async () => {
  const closed: string[] = [];
  const broken: ChatPanelView['items'] = [
    {
      kind: 'question',
      id: 'q',
      get text(): string {
        throw new Error('cannot draw this question');
      },
    },
  ];
  const view = {
    ...viewOf(broken),
    dispatch: (panelEvent: {type: string}) => closed.push(panelEvent.type),
  };
  const {container, unmount} = await mount(<ChatPanel {...view} />);
  expect(container.textContent).toContain('Chat failed');
  await act(async () => container.querySelector('button')?.click());
  expect(closed).toEqual(['close']);
  await unmount();
});
