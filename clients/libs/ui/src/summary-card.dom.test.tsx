/** `SummaryCard` behavior in a browser DOM, through the package's public entry. */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {SummaryCard} from './index.js';

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
  const root = createRoot(container);
  await act(async () => root.render(node));
  return container;
}

function one(container: HTMLElement, selector: string): HTMLElement {
  const element = container.querySelector<HTMLElement>(selector);
  if (element === null) throw new Error(`no ${selector}`);
  return element;
}

async function key(element: HTMLElement, value: string): Promise<void> {
  await act(async () => {
    element.focus();
    element.dispatchEvent(
      new KeyboardEvent('keydown', {key: value, bubbles: true, cancelable: true}),
    );
  });
}

describe('SummaryCard', () => {
  test('has one primary press and presents its complete operator summary', async () => {
    let presses = 0;
    const container = await mount(
      <SummaryCard
        title="Queue throughput"
        description="Finds a faster bounded multi-producer queue."
        latest={{label: 'Latest workstream', text: 'Batch the consumer wake-up path'}}
        chart={<span role="img" aria-label="Throughput increased across seven attempts" />}
        metadata="7 workstreams · $4.20 · 1h 05m"
        status={{tone: 'live', label: 'Running'}}
        attention
        press={() => {
          presses += 1;
        }}
      />,
    );

    const article = one(container, 'article');
    expect(article.hasAttribute('data-attention')).toBe(true);
    const titleId = article.getAttribute('aria-labelledby');
    expect(titleId).not.toBeNull();
    expect(one(container, `[id="${titleId}"]`).textContent).toBe('Queue throughput');
    expect(container.textContent).toContain('Queue throughput');
    expect(container.textContent).toContain('Finds a faster bounded multi-producer queue.');
    expect(container.textContent).toContain('Latest workstream');
    expect(container.textContent).toContain('Batch the consumer wake-up path');
    expect(container.textContent).toContain('7 workstreams · $4.20 · 1h 05m');
    expect(container.textContent).toContain('Running');

    const primary = one(container, '.vs-summary-card-press');
    expect(primary.getAttribute('aria-label')).toBe('Open Queue throughput');
    await act(async () => primary.click());
    expect(presses).toBe(1);
  });

  test('opens the ellipsis menu from any context-menu target without invoking the card', async () => {
    let opened = 0;
    let archived = 0;
    const container = await mount(
      <SummaryCard
        title="Queue throughput"
        description="A run summary"
        latest={{label: 'Latest workstream', text: 'Batch wake-ups'}}
        chart={<span data-chart="true">chart</span>}
        metadata="7 workstreams"
        status={{tone: 'live', label: 'Running'}}
        menu={[
          {
            id: 'archive',
            label: 'Archive',
            press: () => {
              archived += 1;
            },
          },
        ]}
        press={() => {
          opened += 1;
        }}
      />,
    );

    const context = new MouseEvent('contextmenu', {bubbles: true, cancelable: true});
    await act(async () => one(container, '[data-chart]').dispatchEvent(context));
    expect(context.defaultPrevented).toBe(true);
    expect(opened).toBe(0);
    const item = one(container, '[role="menuitem"]');
    expect(item.textContent).toBe('Archive');
    expect(document.activeElement).toBe(item);
    await act(async () => item.click());
    expect(archived).toBe(1);
  });

  test('the visible ellipsis uses the same keyboard menu, including Escape focus return', async () => {
    const container = await mount(
      <SummaryCard
        title="Queue throughput"
        description="A run summary"
        latest={null}
        chart={null}
        metadata="No workstreams yet"
        status={{tone: 'neutral', label: 'Queued'}}
        menu={[{id: 'details', label: 'Open details', press: () => undefined}]}
        press={() => undefined}
      />,
    );

    const trigger = one(container, '[aria-label="More for Queue throughput"]');
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    await key(trigger, 'ArrowDown');
    const item = one(container, '[role="menuitem"]');
    expect(document.activeElement).toBe(item);
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    await key(item, 'Escape');
    expect(container.querySelector('[role="menu"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  test('leaves the native context menu alone when it has no card menu', async () => {
    const container = await mount(
      <SummaryCard
        title="Queue throughput"
        description="A run summary"
        latest={null}
        chart={null}
        metadata="No workstreams yet"
        status={{tone: 'neutral', label: 'Queued'}}
        press={() => undefined}
      />,
    );
    const context = new MouseEvent('contextmenu', {bubbles: true, cancelable: true});
    await act(async () => one(container, 'article').dispatchEvent(context));
    expect(context.defaultPrevented).toBe(false);
  });

  test('draws its figure strip, reasons to look and headline result, and leaves out a missing description', async () => {
    const container = await mount(
      <SummaryCard
        title="Queue throughput"
        description={null}
        latest={null}
        chart={<span data-chart="true">chart</span>}
        metadata="host-a · queue-rs"
        status={{tone: 'live', label: 'Running 2h 0m'}}
        flags={[{label: 'Stalled 40m', tone: 'warn', hint: 'Nothing has moved the run'}]}
        stats={[
          {label: 'Spend', value: '$412', detail: 'of $500', share: 0.82, tone: 'ok'},
          {label: 'Agents', value: '3 working', hint: 'Agents in a turn'},
        ]}
        result={{label: 'Best so far', value: '2.31k tok/s', detail: '71% of bound'}}
        press={() => undefined}
      />,
    );
    expect(container.querySelector('.vs-summary-card-description')).toBeNull();
    const stats = [...container.querySelectorAll('.vs-summary-card-stat')];
    expect(stats.map(each => each.querySelector('.vs-text')?.textContent)).toEqual([
      'Spend',
      'Agents',
    ]);
    expect(stats[0]?.querySelector('.vs-meter')).not.toBeNull();
    expect(stats[1]?.querySelector('.vs-meter')).toBeNull();
    expect(stats[1]?.getAttribute('title')).toBe('Agents in a turn');
    const head = one(container, '.vs-summary-card-head');
    expect(head.textContent).toContain('Running 2h 0m');
    expect(head.textContent).toContain('Stalled 40m');
    expect(one(container, '.vs-summary-card-result').textContent).toContain('2.31k tok/s');
    expect(one(container, '.vs-summary-card-result').textContent).toContain('71% of bound');
  });

  test('draws no figure strip or result when it has none', async () => {
    const container = await mount(
      <SummaryCard
        title="Queue throughput"
        description="A run summary"
        latest={null}
        chart={null}
        metadata="host-a"
        status={{tone: 'neutral', label: 'Queued'}}
        press={() => undefined}
      />,
    );
    expect(container.querySelector('.vs-summary-card-stats')).toBeNull();
    expect(container.querySelector('.vs-summary-card-result')).toBeNull();
  });
});
