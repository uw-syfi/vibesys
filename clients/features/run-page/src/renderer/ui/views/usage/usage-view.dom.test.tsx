/**
 * The Usage face in a browser DOM, through its public props over records the run produced: its
 * totals, the table's rows by the chosen grouping, and each control sending the controls it
 * changes (the page keeps them).
 */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {
  USAGE_CONTROLS_DEFAULT,
  type UsageContext,
  type UsageControls,
  type UsageRecord,
} from '../../../../state/index.js';
import {UsageView} from './usage-view.js';

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

function record(
  role: string,
  harness: string,
  model: string,
  input: number,
  hour: number,
  span: string | null = null,
): UsageRecord {
  return {
    run_id: 'run-1',
    execution_id: null,
    span_id: span,
    round_label: null,
    role,
    harness,
    model,
    reasoning_effort: null,
    harness_version: null,
    started_at: null,
    ended_at: `2026-10-01T0${hour}:00:00Z`,
    tokens: {input, cache_read: input / 2, cache_write: null, output: 10, reasoning: null},
    cost_usd: input / 1000,
    duration_ms: null,
  };
}

const RECORDS = [
  record('planner', 'codex', 'gpt-6-luna', 4000, 1),
  record('implementer', 'codex', 'gpt-6-luna', 2000, 2, 'turn-a'),
  record('implementer', 'claude', 'sonnet-5', 1000, 3, 'turn-b'),
];

const CONTEXT: UsageContext = {
  ideaOfSpan: new Map([['turn-a', 'idea-1']]),
  ideas: new Map(),
  models: new Map([['gpt-6-luna', 'GPT-6 Luna']]),
};

async function mount(
  controls: UsageControls,
  sent: UsageControls[],
  records: readonly UsageRecord[] = RECORDS,
): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  await act(async () =>
    createRoot(container).render(
      <UsageView
        records={records}
        context={CONTEXT}
        controls={controls}
        change={next => void sent.push(next)}
      />,
    ),
  );
  return container;
}

const rows = (container: HTMLElement): string[] =>
  [...container.querySelectorAll('tbody tr')].map(row => row.textContent ?? '');

async function press(element: Element | null): Promise<void> {
  await act(async () => (element as HTMLElement).click());
}

const radio = (container: HTMLElement, group: string, value: string): Element | null =>
  container.querySelector(`[role="radiogroup"][aria-label="${group}"] input[value="${value}"]`);

describe('UsageView', () => {
  test('shows the four tiles, then a row per role by cost and the run total', async () => {
    const view = await mount(USAGE_CONTROLS_DEFAULT, []);
    for (const name of ['Cost', 'Tokens', 'Cached input', 'Per idea']) {
      expect(view.textContent).toContain(name);
    }
    expect(view.textContent).toContain('$7.00');
    expect(rows(view)).toEqual([
      expect.stringMatching(/^Planner/),
      expect.stringMatching(/^Builder/),
      expect.stringMatching(/^Run total.*100%/),
    ]);
  });

  test('says no sentence about its totals or its costs: tiles, labels and numbers only', async () => {
    const unpriced = {...record('planner', 'codex', 'gpt-6-luna', 1000, 4), cost_usd: null};
    const view = await mount(USAGE_CONTROLS_DEFAULT, [], [...RECORDS, unpriced]);
    expect(view.textContent).not.toContain('Totals match');
    expect(view.textContent).not.toContain('reported');
    expect(view.textContent).not.toContain('never zero');
    expect(view.textContent).toContain('1 unpriced');
    const priced = await mount(USAGE_CONTROLS_DEFAULT, []);
    expect(priced.textContent).not.toContain('unpriced');
  });

  test('a model the catalogue names shows its name, and one it does not shows its id', async () => {
    const view = await mount({...USAGE_CONTROLS_DEFAULT, by: 'model'}, []);
    expect(view.textContent).toContain('GPT-6 Luna');
    expect(view.textContent).not.toContain('gpt-6-luna');
    expect(view.textContent).toContain('sonnet-5');
  });

  test('groups by the model, and by the idea with the turns before any idea as Run setup', async () => {
    const byModel = await mount({...USAGE_CONTROLS_DEFAULT, by: 'model'}, []);
    expect(rows(byModel).slice(0, 2)).toEqual([
      expect.stringMatching(/^GPT-6 Luna/),
      expect.stringMatching(/^sonnet-5/),
    ]);
    const byIdea = await mount({...USAGE_CONTROLS_DEFAULT, by: 'idea'}, []);
    expect(rows(byIdea).map(row => row.slice(0, 5))).toEqual(['Run s', 'idea-', 'Run t']);
  });

  test('an unfolded group lists the second grouping beneath it', async () => {
    const sent: UsageControls[] = [];
    const open = await mount(
      {...USAGE_CONTROLS_DEFAULT, within: 'model', open: ['"implementer"']},
      sent,
    );
    expect(rows(open).map(row => row.slice(0, 12))).toEqual([
      expect.stringMatching(/^Planner/),
      expect.stringMatching(/^Builder/),
      expect.stringMatching(/^GPT-6 Luna/),
      expect.stringMatching(/^sonnet-5/),
      expect.stringMatching(/^Run total/),
    ]);
    await press(open.querySelector('button[aria-label="Collapse Builder"]'));
    expect(sent).toEqual([{...USAGE_CONTROLS_DEFAULT, within: 'model', open: []}]);
  });

  test('each control sends the controls it changes', async () => {
    const sent: UsageControls[] = [];
    const view = await mount(USAGE_CONTROLS_DEFAULT, sent);
    await press(radio(view, 'Group by', 'harness'));
    await press(radio(view, 'Count', 'tokens'));
    await press(radio(view, 'Chart', 'running'));
    await press([...view.querySelectorAll('tbody tr')][0] ?? null);
    const select = view.querySelector('select') as HTMLSelectElement;
    await act(async () => {
      select.value = 'model';
      select.dispatchEvent(new window.Event('change', {bubbles: true}) as unknown as Event);
    });
    expect(sent).toEqual([
      {...USAGE_CONTROLS_DEFAULT, by: 'harness'},
      {...USAGE_CONTROLS_DEFAULT, metric: 'tokens'},
      {...USAGE_CONTROLS_DEFAULT, chart: 'running'},
      {...USAGE_CONTROLS_DEFAULT, highlight: '"planner"'},
      {...USAGE_CONTROLS_DEFAULT, within: 'model'},
    ]);
  });

  test('the token kinds show with the Tokens metric, and the last one cannot be switched off', async () => {
    const sent: UsageControls[] = [];
    const view = await mount(
      {...USAGE_CONTROLS_DEFAULT, metric: 'tokens', kinds: ['cached']},
      sent,
    );
    expect(view.textContent).toContain('Reasoning');
    const boxes = [...view.querySelectorAll('input[type="checkbox"]')];
    expect(boxes).toHaveLength(4);
    await press(boxes[1] ?? null);
    await press(boxes[2] ?? null);
    expect(sent.map(next => next.kinds)).toEqual([['cached'], ['cached', 'output']]);
  });

  test('says there is no usage when the run has no records', async () => {
    const view = await mount(USAGE_CONTROLS_DEFAULT, [], []);
    expect(view.textContent).toContain('No usage yet');
    expect(view.querySelector('table')).toBeNull();
  });
});
