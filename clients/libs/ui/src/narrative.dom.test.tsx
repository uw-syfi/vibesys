/** The turn narrative's blocks and the icon set in a DOM, through `.` only. */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import * as fc from 'fast-check';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {ICON_NAMES, Icon, Narrative, Said, Thought, ToolGroup, type ToolLine} from './index.js';

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

async function mount(node: ReactNode): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  await act(async () => createRoot(container).render(node));
  return container;
}

async function click(element: Element): Promise<void> {
  await act(async () => {
    element.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
  });
}

function all(container: HTMLElement, selector: string): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>(selector)];
}

function one(container: HTMLElement, selector: string): HTMLElement {
  const element = container.querySelector<HTMLElement>(selector);
  if (element === null) throw new Error(`no ${selector}`);
  return element;
}

const noop = (): void => undefined;

const REASONING = '**Looking at the benchmark**\n\nThe head and tail counters share a cache line.';

describe('Thought', () => {
  test('closed, a finished thought reads "Thought" with a brain and a one-line preview', async () => {
    const container = await mount(<Thought text={REASONING} open={false} toggle={noop} />);
    const head = one(container, '.vs-collapsible-head');
    expect(one(head, '.vs-collapsible-label').textContent).toBe('Thought');
    expect(head.querySelector('.vs-thought-icon svg')).not.toBeNull();
    const preview = one(head, '.vs-collapsible-summary').textContent ?? '';
    expect(preview).toBe(
      'Looking at the benchmark · The head and tail counters share a cache line.',
    );
    expect(head.getAttribute('aria-expanded')).toBe('false');
    expect(container.querySelector('.vs-collapsible-body')).toBeNull();
    expect(container.querySelector('.vs-thought[data-live]')).toBeNull();
  });

  test('open, it shows its text as Markdown and drops the preview', async () => {
    const container = await mount(<Thought text={REASONING} open toggle={noop} />);
    expect(container.querySelector('.vs-collapsible-summary')).toBeNull();
    expect(one(container, '.vs-collapsible-body strong').textContent).toBe(
      'Looking at the benchmark',
    );
    expect(one(container, '.vs-collapsible-head').getAttribute('aria-expanded')).toBe('true');
  });

  test('while live it reads "Thinking" and says so on the block', async () => {
    const container = await mount(<Thought live text={REASONING} open toggle={noop} />);
    expect(one(container, '.vs-collapsible-label').textContent).toBe('Thinking');
    expect(one(container, '.vs-thought').getAttribute('data-live')).toBe('true');
  });

  test('pressing its line asks for the opposite of how it stands', async () => {
    const asked: boolean[] = [];
    const closed = await mount(
      <Thought text={REASONING} open={false} toggle={on => asked.push(on)} />,
    );
    await click(one(closed, '.vs-collapsible-head'));
    const open = await mount(<Thought text={REASONING} open toggle={on => asked.push(on)} />);
    await click(one(open, '.vs-collapsible-head'));
    expect(asked).toEqual([true, false]);
  });

  test('previews only the start of a long thought, so a long one costs no more than a short one', async () => {
    const long = `**Title**\n\n${'Words that go on and on. '.repeat(20_000)}`;
    const container = await mount(<Thought text={long} open={false} toggle={noop} />);
    const head = one(container, '.vs-collapsible-head');
    const preview = one(head, '.vs-collapsible-summary').textContent ?? '';
    expect(preview.startsWith('Title · Words that go on')).toBe(true);
    expect(preview.length).toBeLessThan(800);
    expect(head.getAttribute('title')?.length).toBeLessThan(900);
    expect((container.textContent ?? '').length).toBeLessThan(1000);
  });

  test('a preview is one line of at most a few hundred characters, whatever the text', () => {
    fc.assert(
      fc.property(
        fc.array(fc.string({unit: 'grapheme', maxLength: 60}), {maxLength: 40}),
        fc.constantFrom('\n', '\n\n', '\r\n\r\n', ' \n \n'),
        (parts, separator) => {
          const container = document.createElement('div');
          document.body.append(container);
          const root = createRoot(container);
          act(() =>
            root.render(<Thought text={parts.join(separator)} open={false} toggle={noop} />),
          );
          const preview = container.querySelector('.vs-collapsible-summary')?.textContent ?? '';
          expect(preview).not.toMatch(/[\r\n]/);
          expect(preview.length).toBeLessThan(800);
          act(() => root.unmount());
        },
      ),
    );
  });

  test('has no preview for text with no words', async () => {
    const container = await mount(<Thought text="**" open={false} toggle={noop} />);
    expect(container.querySelector('.vs-collapsible-summary')).toBeNull();
  });
});

function line(id: string, patch: Partial<ToolLine> = {}): ToolLine {
  return {id, name: 'Bash', line: `run ${id}`, state: 'ok', press: noop, ...patch};
}

describe('ToolGroup rows', () => {
  test('each call is a row with a status icon, a name chip, its subject, its outcome and its time', async () => {
    const container = await mount(
      <ToolGroup
        tools={[
          line('a', {outcome: 'exit 0 · 2.3s', time: '12s'}),
          line('b', {state: 'failed', outcome: 'exit 1 · 0.4s', time: '14s'}),
          line('c', {state: 'pending', time: '20s'}),
        ]}
        open
        toggle={noop}
      />,
    );
    const rows = all(container, '.vs-toolgroup-row');
    expect(rows.map(row => row.getAttribute('data-state'))).toEqual(['ok', 'failed', 'pending']);
    expect(
      rows.map(row => one(row, '.vs-toolgroup-status svg').getAttribute('aria-label')),
    ).toEqual(['Done', 'Failed', 'In progress']);
    expect(rows.map(row => one(row, '.vs-toolgroup-name').textContent)).toEqual([
      'Bash',
      'Bash',
      'Bash',
    ]);
    expect(rows.map(row => one(row, '.vs-toolgroup-line').textContent)).toEqual([
      'run a',
      'run b',
      'run c',
    ]);
    expect(rows.map(row => row.querySelector('.vs-toolgroup-outcome')?.textContent)).toEqual([
      'exit 0 · 2.3s',
      'exit 1 · 0.4s',
      undefined,
    ]);
    expect(rows.map(row => one(row, '.vs-toolgroup-time').textContent)).toEqual([
      '12s',
      '14s',
      '20s',
    ]);
  });

  test('the time says what it is when hovered', async () => {
    const container = await mount(
      <ToolGroup
        tools={[line('a', {time: '+1m 17s', timeTitle: '1m 17s into the turn'})]}
        open
        toggle={noop}
      />,
    );
    const time = one(container, '.vs-toolgroup-time');
    expect(time.textContent).toBe('+1m 17s');
    expect(time.getAttribute('title')).toBe('1m 17s into the turn');
  });

  test('a row with no time draws none', async () => {
    const container = await mount(<ToolGroup tools={[line('a')]} open toggle={noop} />);
    expect(container.querySelector('.vs-toolgroup-time')).toBeNull();
  });

  test('open, the head only counts the calls; closed, it lists them and keeps a failed row', async () => {
    const tools = [line('a'), line('b', {state: 'failed'})];
    const open = await mount(<ToolGroup tools={tools} open toggle={noop} />);
    expect(open.querySelector('.vs-toolgroup-head .vs-toolgroup-line')).toBeNull();
    const closed = await mount(<ToolGroup tools={tools} open={false} toggle={noop} />);
    expect(one(closed, '.vs-toolgroup-head .vs-toolgroup-line').textContent).toContain('run a');
    expect(all(closed, '.vs-toolgroup-row').map(row => row.getAttribute('data-state'))).toEqual([
      'failed',
    ]);
  });

  test('thirty calls of every state keep every failed call visible while closed', async () => {
    const states = ['ok', 'failed', 'pending'] as const;
    const tools = Array.from({length: 30}, (_, index) =>
      line(`t${index}`, {state: states[index % 3] as ToolLine['state']}),
    );
    const container = await mount(<ToolGroup tools={tools} open={false} toggle={noop} />);
    expect(one(container, '.vs-toolgroup-count').textContent).toBe('30 tools');
    expect(all(container, '.vs-toolgroup-row')).toHaveLength(10);
    expect(
      all(container, '.vs-toolgroup-row').every(row => row.getAttribute('data-state') === 'failed'),
    ).toBe(true);
  });
});

describe('Icon', () => {
  test('every name in the closed set draws one icon, hidden unless it has a label', async () => {
    for (const name of ICON_NAMES) {
      const plain = await mount(<Icon name={name} />);
      const icon = one(plain, 'svg');
      expect(icon.getAttribute('class')).toContain('vs-icon');
      expect(icon.getAttribute('aria-hidden')).toBe('true');
      expect(icon.getAttribute('role')).toBeNull();
      const named = await mount(<Icon name={name} label={`the ${name}`} />);
      const labelled = one(named, 'svg');
      expect(labelled.getAttribute('role')).toBe('img');
      expect(labelled.getAttribute('aria-label')).toBe(`the ${name}`);
      expect(labelled.getAttribute('aria-hidden')).toBeNull();
    }
  });

  test('has an icon for a thought, a command, a file, an edit, a search, the web, an agent, a tool, done and a failure', () => {
    for (const name of [
      'brain',
      'terminal',
      'file',
      'pencil',
      'search',
      'globe',
      'bot',
      'wrench',
      'check',
      'alert',
    ] as const) {
      expect(ICON_NAMES).toContain(name);
    }
    expect(new Set(ICON_NAMES).size).toBe(ICON_NAMES.length);
  });
});

describe('Narrative with the new blocks', () => {
  test('keeps a thought, what was said and a group of rows in the order given', async () => {
    const container = await mount(
      <Narrative label="Turn">
        <Thought key="t" text="hmm" open={false} toggle={noop} />
        <Said key="s" text="then" />
        <ToolGroup key="g" tools={[line('a')]} open toggle={noop} />
      </Narrative>,
    );
    expect(
      all(container, '.vs-thought, .vs-said, .vs-toolgroup').map(block => block.className),
    ).toEqual(['vs-thought', 'vs-said', 'vs-toolgroup']);
  });
});
