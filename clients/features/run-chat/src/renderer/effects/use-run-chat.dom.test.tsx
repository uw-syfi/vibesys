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
import {
  type AskInput,
  type DeliverRequest,
  errorQuestion,
  STEER_MODES,
  steerHelp,
} from '../../state/index.js';
import {ChatPanel} from '../ui/chat-panel.js';
import type {RunChatPorts} from './ports.js';
import {type RunChatHost, useRunChat} from './use-run-chat.js';

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
    maxQuestionBytes: 65536,
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

test('New steer turns the typed text into a card whose target is picked from the run roles', async () => {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const sent: DeliverRequest[] = [];
  function WithRoles(): JSX.Element {
    const chat = useRunChat({
      identity: 'run',
      core: fold([THREAD]),
      ports: fakePorts(sent),
      roles: ['orchestrator', 'implementer'],
    });
    return (
      <div>
        <button type="button" onClick={chat.toggle}>
          Chat
        </button>
        {chat.panel === null ? null : <ChatPanel {...chat.panel} />}
      </div>
    );
  }
  await act(async () => root.render(<WithRoles />));
  await act(async () => press(container, 'Chat'));
  // One input, one send control (an icon, so no word of its own), and no author labels.
  expect(container.querySelectorAll('textarea')).toHaveLength(1);
  expect(container.querySelector('textarea')?.getAttribute('placeholder')).toBe(
    'Ask about this run',
  );
  expect(container.querySelector('button[aria-label="Ask"]')?.textContent).toBe('');
  await act(async () => type(container.querySelector('textarea') as Element, 'pad the counters'));
  await act(async () => press(container, 'New steer'));
  const select = container.querySelector('select') as HTMLSelectElement;
  const options = [...select.querySelectorAll('option')].map(each => each.textContent);
  expect(options).toEqual(['Whole run', 'Planner', 'Implementer']);
  expect(sent).toEqual([]);
  await act(async () => {
    select.value = 'role:implementer';
    select.dispatchEvent(new window.Event('change', {bubbles: true}) as unknown as Event);
  });
  await act(async () => press(container, 'Send'));
  expect(sent).toEqual([
    {
      control_id: 'control-1',
      action: 'steer',
      text: 'pad the counters',
      target: {kind: 'role', id: 'implementer'},
      mode: 'next_turn',
    },
  ]);
  await act(async () => root.unmount());
});

test('the steer card says what each When choice does for its To choice, under the choices', async () => {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const sent: DeliverRequest[] = [];
  function WithRoles(): JSX.Element {
    const chat = useRunChat({
      identity: 'run',
      core: fold([THREAD]),
      ports: fakePorts(sent),
      roles: ['implementer'],
    });
    return (
      <div>
        <button type="button" onClick={chat.toggle}>
          Chat
        </button>
        {chat.panel === null ? null : <ChatPanel {...chat.panel} />}
      </div>
    );
  }
  await act(async () => root.render(<WithRoles />));
  await act(async () => press(container, 'Chat'));
  await act(async () => press(container, 'New steer'));
  const select = container.querySelector('select') as HTMLSelectElement;
  for (const target of [
    {kind: 'role', id: 'implementer'},
    {kind: 'run', id: null},
  ] as const) {
    await act(async () => {
      select.value = target.kind === 'run' ? 'run' : `role:${target.id}`;
      select.dispatchEvent(new window.Event('change', {bubbles: true}) as unknown as Event);
    });
    // From the last choice back to the first, so every choice is a change.
    for (const {value} of [...STEER_MODES].reverse()) {
      const choice = container.querySelector(`input[type="radio"][value="${value}"]`);
      await act(async () => (choice as HTMLInputElement).click());
      expect((choice as HTMLInputElement).checked).toBe(true);
      for (const {value: mode} of STEER_MODES)
        expect(
          container.textContent?.includes(steerHelp({mode, target})),
          `${mode} under ${value} to ${target.kind}`,
        ).toBe(mode === value);
    }
  }
  expect(sent).toEqual([]);
  await act(async () => root.unmount());
});

for (const how of ['Enter', 'Ask'] as const) {
  test(`what is typed goes out as a question on ${how}, never as a steer`, async () => {
    const container = document.createElement('div');
    document.body.append(container);
    const root = createRoot(container);
    const sent: DeliverRequest[] = [];
    const asked: AskInput[] = [];
    const ports: RunChatPorts = {
      ...fakePorts(sent),
      ask: input => {
        asked.push(input);
        return Promise.resolve({ok: true});
      },
    };
    await act(async () => root.render(<Harness core={fold([THREAD])} ports={ports} />));
    await act(async () => press(container, 'Chat'));
    const box = container.querySelector('textarea') as HTMLTextAreaElement;
    await act(async () => type(box, "What's the progress right now?"));
    await act(async () => {
      if (how === 'Enter')
        box.dispatchEvent(
          new window.KeyboardEvent('keydown', {key: 'Enter', bubbles: true}) as unknown as Event,
        );
      else (container.querySelector('button[aria-label="Ask"]') as HTMLElement).click();
    });
    await act(async () => undefined);
    expect(asked.map(each => [each.threadId, each.text])).toEqual([
      ['t', "What's the progress right now?"],
    ]);
    expect(sent).toEqual([]);
    expect(container.querySelector('[role="radiogroup"]')).toBeNull();
    await act(async () => root.unmount());
  });
}

function started(
  sequence: number,
  id: string,
  role: string,
  provider: string,
  model: string,
): RunEvent {
  return event(sequence, 'agent_execution_started', {
    execution_id: id,
    agent_kind: role,
    round_label: 'round-1',
    status: 'active',
    data: {
      kind: 'agent_execution_started',
      stage: 'planning',
      attempt: 1,
      system_prompt: '',
      user_prompt: 'go',
      provider,
      model,
      activity: {
        kind: 'agent_execution_activity_changed',
        mode: 'thinking',
        summary: '',
        tool: null,
      },
    },
  });
}

test('a new chat starts on the harness and model the run runs on, not the first provider', async () => {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const ports: RunChatPorts = {
    ...fakePorts([]),
    harnesses: () =>
      Promise.resolve({
        kind: 'listed',
        harnesses: [
          {
            provider: 'claude',
            model: 'opus',
            default: true,
            display: {providerName: 'Claude Code', modelName: 'Opus'},
          },
          {
            provider: 'codex',
            model: 'gpt-6-luna',
            default: false,
            display: {providerName: 'Codex', modelName: 'GPT-6 Luna'},
          },
          {
            provider: 'codex',
            model: 'gpt-6-sol',
            default: true,
            display: {providerName: 'Codex', modelName: 'GPT-6 Sol'},
          },
        ],
      }),
  };
  const core = fold([
    started(10, 'e1', 'implementer', 'claude', 'opus'),
    started(11, 'e2', 'planner', 'codex', 'gpt-6-luna'),
  ]);
  await act(async () => root.render(<Harness core={core} ports={ports} />));
  await act(async () => press(container, 'Chat'));
  await act(async () => undefined);
  const selects = [...container.querySelectorAll('select')] as HTMLSelectElement[];
  expect(selects.map(select => select.selectedOptions[0]?.textContent)).toEqual([
    'Codex',
    'GPT-6 Luna',
  ]);
  await act(async () => root.unmount());
});

function chunk(sequence: number, thread: string, text: string): RunEvent {
  return event(sequence, 'agent_output_chunk', {
    ...CHAT,
    chat_thread_id: thread,
    invocation_id: `turn-${sequence}`,
    data: {kind: 'agent_output_chunk', channel: 'assistant', content: text},
  });
}

function untitled(sequence: number, thread: string): RunEvent {
  return event(sequence, 'chat_thread_created', {
    ...CHAT,
    chat_thread_id: thread,
    data: {
      kind: 'chat_thread_created',
      thread_id: thread,
      title: '',
      provider: 'claude',
      model: 'opus',
      created_at: '2026-01-01T00:00:00Z',
    },
  });
}

test('New chat opens an empty thread, and no thread in the Threads menu goes by that name', async () => {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const core = fold([THREAD, chunk(2, 't', 'Still writing the answer'), untitled(3, 'u')]);
  await act(async () => root.render(<Harness core={core} ports={fakePorts([])} />));
  await act(async () => press(container, 'Chat'));
  await act(async () => press(container, 'Threads'));
  const entries = [...container.querySelectorAll('[role="menuitem"]')].map(each =>
    each.textContent?.trim(),
  );
  expect(entries).toHaveLength(2);
  expect(entries).not.toContain('New chat');
  await act(async () => press(container, 'New chat'));
  expect(container.textContent).not.toContain('Still writing the answer');
  expect(container.textContent).not.toContain('Writing');
  expect(container.textContent).not.toContain('Working');
  await act(async () => root.unmount());
});

test('asking about an error starts a new thread with the question and opens the panel', async () => {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const created: string[] = [];
  const asked: AskInput[] = [];
  const ports: RunChatPorts = {
    ...fakePorts([]),
    createThread: (_harness, _signal, requestId) => {
      created.push(requestId);
      return Promise.resolve({ok: true, threadId: 't-new'});
    },
    ask: input => {
      asked.push(input);
      return Promise.resolve({ok: true});
    },
  };
  let host: RunChatHost | null = null;
  function Probe(): JSX.Element {
    const chat = useRunChat({identity: 'run', core: fold([THREAD]), ports});
    host = chat;
    return chat.panel === null ? <div /> : <ChatPanel {...chat.panel} />;
  }
  await act(async () => root.render(<Probe />));
  const report = {
    where: 'Run ended: Failed',
    message: 'Git refused the workspace',
    detail: 'fatal: not a work tree',
  };
  expect((host as RunChatHost | null)?.open).toBe(false);
  await act(async () => (host as RunChatHost | null)?.askAboutError?.(report));
  await act(async () => undefined);
  expect((host as RunChatHost | null)?.open).toBe(true);
  expect(created).toHaveLength(1);
  expect(asked).toHaveLength(1);
  expect(asked[0]?.threadId).toBe('t-new');
  expect(asked[0]?.text).toBe(errorQuestion(report, 65536));
  await act(async () => root.unmount());
});
