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
): UsageRecord {
  return {
    run_id: 'run-1',
    execution_id: null,
    span_id: null,
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
  record('implementer', 'codex', 'gpt-6-luna', 2000, 2),
  record('implementer', 'claude', 'sonnet-5', 1000, 3),
];

async function mount(
  records: readonly UsageRecord[],
  controls: UsageControls,
  sent: UsageControls[],
): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  await act(async () =>
    createRoot(container).render(
      <UsageView records={records} controls={controls} change={next => void sent.push(next)} />,
    ),
  );
  return container;
}

const rows = (container: HTMLElement): string[] =>
  [...container.querySelectorAll('tbody tr')].map(row => row.textContent ?? '');

describe('UsageView', () => {
  test('totals the run, then lists a row per role, the most input first', async () => {
    const view = await mount(RECORDS, USAGE_CONTROLS_DEFAULT, []);
    expect(view.textContent).toContain('· 3 turns');
    expect(view.textContent).toContain('· cost $7.00');
    expect(rows(view)).toEqual([
      expect.stringMatching(/^Planner4k/),
      expect.stringMatching(/^Implementer3k/),
    ]);
  });

  test('groups by the model when asked, and by a second dimension within it', async () => {
    const view = await mount(RECORDS, {...USAGE_CONTROLS_DEFAULT, by: 'model', within: 'role'}, []);
    expect(rows(view).map(row => row.slice(0, 26))).toEqual([
      expect.stringContaining('gpt-6-luna·Planner'),
      expect.stringContaining('gpt-6-luna·Implementer'),
      expect.stringContaining('sonnet-5·Implementer'),
    ]);
  });

  test('each control sends the controls it changes', async () => {
    const sent: UsageControls[] = [];
    const view = await mount(RECORDS, USAGE_CONTROLS_DEFAULT, sent);
    const select = (label: string): HTMLSelectElement => {
      const found = [...view.querySelectorAll('label')].find(each => each.textContent === label);
      const id = found?.getAttribute('for') ?? '';
      return view.querySelector<HTMLSelectElement>(`select[id="${id}"]`) as HTMLSelectElement;
    };
    await act(async () => {
      const by = select('Group by');
      by.value = 'harness';
      by.dispatchEvent(new window.Event('change', {bubbles: true}) as unknown as Event);
    });
    await act(async () => {
      const within = select('Then by');
      within.value = 'model';
      within.dispatchEvent(new window.Event('change', {bubbles: true}) as unknown as Event);
    });
    await act(async () => (view.querySelector('input[value="cost"]') as HTMLInputElement).click());
    expect(sent).toEqual([
      {...USAGE_CONTROLS_DEFAULT, by: 'harness'},
      {...USAGE_CONTROLS_DEFAULT, within: 'model'},
      {...USAGE_CONTROLS_DEFAULT, metric: 'cost'},
    ]);
  });

  test('says there is no usage when the run has no records', async () => {
    const view = await mount([], USAGE_CONTROLS_DEFAULT, []);
    expect(view.textContent).toContain('No usage yet');
    expect(view.querySelector('table')).toBeNull();
  });
});
