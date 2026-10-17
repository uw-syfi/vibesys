/** The thread, message, tool-call and collapsible components in a browser DOM, through `.` only. */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act, type ReactNode, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {
  Collapsible,
  Message,
  type ReplyField,
  StructuredReply,
  Thread,
  type ThreadItem,
  ToolCall,
} from './index.js';

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

interface Mounted {
  readonly container: HTMLElement;
  readonly render: (node: ReactNode) => Promise<void>;
}

async function mount(node: ReactNode): Promise<Mounted> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const render = (next: ReactNode): Promise<void> => act(async () => root.render(next));
  await render(node);
  return {container, render};
}

async function click(element: Element): Promise<void> {
  await act(async () => {
    (element as HTMLElement).click();
  });
}

function one(container: HTMLElement, selector: string): HTMLElement {
  const element = container.querySelector<HTMLElement>(selector);
  if (element === null) throw new Error(`no ${selector}`);
  return element;
}

function button(container: HTMLElement, name: string): HTMLElement {
  const found = [...container.querySelectorAll<HTMLElement>('button')].find(candidate =>
    (candidate.textContent ?? '').includes(name),
  );
  if (found === undefined) throw new Error(`no button named ${name}`);
  return found;
}

const text = (container: HTMLElement): string => container.textContent ?? '';

function Closed(props: {readonly children: ReactNode}) {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible label="Block" summary="preview" open={open} toggle={setOpen}>
      {props.children}
    </Collapsible>
  );
}

describe('Collapsible', () => {
  test('opens and closes by its button, which says which it is', async () => {
    const {container} = await mount(<Closed>inside</Closed>);
    const head = button(container, 'Block');
    expect(head.getAttribute('aria-expanded')).toBe('false');
    expect(text(container)).not.toContain('inside');
    await click(head);
    expect(head.getAttribute('aria-expanded')).toBe('true');
    expect(text(container)).toContain('inside');
    await click(head);
    expect(text(container)).not.toContain('inside');
  });
});

describe('Message', () => {
  test('previews its first line closed and shows all of its text open', async () => {
    const lines = 'first line\nsecond line';
    const closed = await mount(
      <Message
        author="Worker"
        variant="reply"
        text={lines}
        open={false}
        toggle={() => undefined}
      />,
    );
    expect(text(closed.container)).toContain('first line');
    expect(text(closed.container)).not.toContain('second line');
    const open = await mount(
      <Message author="Worker" variant="reply" text={lines} open toggle={() => undefined} />,
    );
    expect(one(open.container, '.vs-message-text').textContent).toBe(lines);
  });

  test('shows text as text, never as markup', async () => {
    const {container} = await mount(
      <Message
        author="Worker"
        variant="reply"
        text="<img src=x onerror=alert(1)>"
        open
        toggle={() => undefined}
      />,
    );
    expect(container.querySelector('img')).toBeNull();
    expect(text(container)).toContain('<img src=x');
  });
});

describe('ToolCall', () => {
  const props = {name: 'Read', input: '{"path":"a"}', toggle: () => undefined};

  test('shows its input and result when open, and neither when closed', async () => {
    const closed = await mount(<ToolCall {...props} state="ok" output="done" open={false} />);
    expect(text(closed.container)).not.toContain('{"path":"a"}');
    const open = await mount(<ToolCall {...props} state="ok" output="done" open />);
    expect(text(open.container)).toContain('{"path":"a"}');
    expect(text(open.container)).toContain('done');
  });

  test('says it is waiting for a result that has not come, and names an error as one', async () => {
    const pending = await mount(<ToolCall {...props} state="pending" output={null} open />);
    expect(text(pending.container)).toContain('Waiting for the result');
    const failed = await mount(<ToolCall {...props} state="failed" output="boom" open />);
    expect(text(failed.container)).toContain('Error');
    expect(failed.container.querySelector('[aria-label="Failed"]')).not.toBeNull();
  });
});

function items(count: number): ThreadItem[] {
  return Array.from({length: count}, (_, index) =>
    index % 3 === 0
      ? {
          kind: 'message' as const,
          id: `m${index}`,
          author: 'Worker',
          variant: index === 0 ? ('reasoning' as const) : ('reply' as const),
          text: `message ${index}`,
        }
      : {
          kind: 'tool' as const,
          id: `t${index}`,
          name: `tool${index}`,
          state: 'ok' as const,
          input: null,
          output: `output ${index}`,
        },
  );
}

/** Gives the thread's scroll box a geometry, which a DOM without layout has none of. */
function laidOut(container: HTMLElement, scrollHeight: number): HTMLElement {
  const pane = one(container, '.vs-thread-pane');
  Object.defineProperty(pane, 'scrollHeight', {configurable: true, value: scrollHeight});
  Object.defineProperty(pane, 'clientHeight', {configurable: true, value: 480});
  return pane;
}

async function scrollTo(pane: HTMLElement, top: number): Promise<void> {
  await act(async () => {
    pane.scrollTop = top;
    pane.dispatchEvent(new UIEvent('scroll', {bubbles: true}));
  });
}

describe('Thread', () => {
  const COUNT = 3000;

  test('draws a window of a long thread, at its end, whatever its length', async () => {
    const {container} = await mount(<Thread label="Turns" items={items(COUNT)} />);
    const rows = container.querySelectorAll('.vs-thread-row');
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.length).toBeLessThan(60);
    const last = rows[rows.length - 1];
    expect(last?.getAttribute('aria-posinset')).toBe(String(COUNT));
    expect(last?.getAttribute('aria-setsize')).toBe(String(COUNT));
  });

  test('follows new items while at the end, and stops following once scrolled up', async () => {
    const view = await mount(<Thread label="Turns" items={items(COUNT)} />);
    const pane = laidOut(view.container, 100000);
    await view.render(<Thread label="Turns" items={items(COUNT + 3)} />);
    let rows = view.container.querySelectorAll('.vs-thread-row');
    expect(rows[rows.length - 1]?.getAttribute('aria-posinset')).toBe(String(COUNT + 3));
    expect(text(view.container)).not.toContain('New below');

    await scrollTo(pane, 0);
    rows = view.container.querySelectorAll('.vs-thread-row');
    expect(rows[0]?.getAttribute('aria-posinset')).toBe('1');
    expect(text(view.container)).toContain('New below');
    await view.render(<Thread label="Turns" items={items(COUNT + 6)} />);
    rows = view.container.querySelectorAll('.vs-thread-row');
    expect(rows[0]?.getAttribute('aria-posinset')).toBe('1');
    await click(button(view.container, 'New below'));
    rows = view.container.querySelectorAll('.vs-thread-row');
    expect(rows[rows.length - 1]?.getAttribute('aria-posinset')).toBe(String(COUNT + 6));
  });

  test('keeps a block open or closed as the reader left it after it scrolls away and back', async () => {
    const view = await mount(<Thread label="Turns" items={items(COUNT)} />);
    const pane = laidOut(view.container, 100000);
    await scrollTo(pane, 0);
    const first = (): HTMLElement => one(view.container, '[aria-posinset="1"] button');
    expect(first().getAttribute('aria-expanded')).toBe('false');
    await click(first());
    expect(first().getAttribute('aria-expanded')).toBe('true');
    await scrollTo(pane, 99000);
    expect(view.container.querySelector('[aria-posinset="1"]')).toBeNull();
    await scrollTo(pane, 0);
    expect(first().getAttribute('aria-expanded')).toBe('true');
  });

  test('draws every item of a short thread', async () => {
    const {container} = await mount(<Thread label="Turns" items={items(5)} />);
    expect(container.querySelectorAll('.vs-thread-row').length).toBe(5);
  });
});

const FIELDS: readonly ReplyField[] = [
  {kind: 'badge', key: 'verdict', label: 'Verdict', value: 'pass', tone: 'ok'},
  {kind: 'prose', key: 'analysis', label: 'Analysis', text: 'The edit is sound.'},
  {kind: 'list', key: 'checks', label: 'Checks', items: ['build', 'tests']},
  {
    kind: 'group',
    key: 'skill',
    label: 'Skill',
    fields: [{kind: 'fact', key: 'name', label: 'Name', value: 'rocprof'}],
  },
];

function Reply(props: {readonly open?: boolean}) {
  const [open, setOpen] = useState(props.open ?? true);
  return (
    <StructuredReply
      author="Judge"
      title="Judge response"
      fields={FIELDS}
      raw={'{\n  "verdict": "pass"\n}'}
      open={open}
      toggle={setOpen}
    />
  );
}

describe('StructuredReply', () => {
  test('shows text as text, a choice as a badge, and a list as a list, with no braces', async () => {
    const {container} = await mount(<Reply />);
    expect(text(container)).toContain('Judge response');
    expect(text(container)).toContain('pass');
    expect(text(container)).toContain('The edit is sound.');
    expect([...container.querySelectorAll('li')].map(item => item.textContent)).toEqual([
      'build',
      'tests',
    ]);
    expect(text(container)).not.toContain('{');
  });

  test('opens a nested record on request and shows the raw JSON behind a toggle', async () => {
    const {container} = await mount(<Reply />);
    expect(text(container)).not.toContain('rocprof');
    await click(button(container, 'Skill'));
    expect(text(container)).toContain('rocprof');
    expect(text(container)).not.toContain('"verdict"');
    const raw = button(container, 'Raw');
    expect(raw.getAttribute('aria-expanded')).toBe('false');
    await click(raw);
    expect(raw.getAttribute('aria-expanded')).toBe('true');
    expect(text(container)).toContain('"verdict": "pass"');
  });

  test('closed, it keeps its badges on the title line and hides its fields', async () => {
    const {container} = await mount(<Reply open={false} />);
    expect(text(container)).toContain('pass');
    expect(text(container)).not.toContain('The edit is sound.');
  });
});

describe('StructuredReply with an overview', () => {
  function Brief() {
    const [open, setOpen] = useState(true);
    return (
      <StructuredReply
        author="Judge"
        title="Judge response"
        fields={FIELDS}
        raw="{}"
        open={open}
        toggle={setOpen}
        overview={['analysis']}
      />
    );
  }

  test('shows its choices and the overview fields, and the rest on "Show all fields"', async () => {
    const {container} = await mount(<Brief />);
    expect(text(container)).toContain('pass');
    expect(text(container)).toContain('The edit is sound.');
    expect(container.querySelectorAll('li')).toHaveLength(0);
    expect(container.textContent).not.toContain('Raw');
    const more = button(container, 'Show all fields (2 more)');
    expect(more.getAttribute('aria-expanded')).toBe('false');
    await click(more);
    expect([...container.querySelectorAll('li')]).toHaveLength(2);
    expect(text(container)).toContain('Raw');
    await click(button(container, 'Show fewer fields'));
    expect(container.querySelectorAll('li')).toHaveLength(0);
  });
});

describe('a tool call, closed and open', () => {
  const long = Array.from({length: 60}, (_, index) => `line ${index}`).join('\n');

  test('is one line closed: its name, its main argument and its result size', async () => {
    const {container} = await mount(
      <ToolCall
        name="Read"
        state="ok"
        summary="src/lib.rs"
        meta="60 lines"
        input='{"file_path":"src/lib.rs"}'
        output={long}
        open={false}
        toggle={() => undefined}
      />,
    );
    expect(text(container)).toContain('Read');
    expect(text(container)).toContain('src/lib.rs');
    expect(text(container)).toContain('60 lines');
    expect(text(container)).not.toContain('line 0');
  });

  test('shows the start of a long result open, and all of it on "Show more"', async () => {
    const {container} = await mount(
      <ToolCall name="Read" state="ok" input={null} output={long} open toggle={() => undefined} />,
    );
    expect(text(container)).toContain('line 19');
    expect(text(container)).not.toContain('line 40');
    await click(button(container, 'Show more'));
    expect(text(container)).toContain('line 59');
    expect(text(container)).not.toContain('Show more');
  });
});
