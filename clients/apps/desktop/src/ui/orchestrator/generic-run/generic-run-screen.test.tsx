import {describe, expect, it} from '@effect/vitest';
import type {RunDetailDelivery} from '../../../core/index.js';
import type {GenericRunEntry} from '../../../state/index.js';
import {installDom, mount} from '../../../testing/dom/harness.js';
import {
  GENERIC_RUN_DETAIL_FIXTURES,
  genericRunDetailFixture,
} from '../../../testing/run-detail/index.js';
import {GenericRunScreen} from './generic-run-screen.js';

installDom();

const run = (overrides: Partial<GenericRunEntry> = {}): GenericRunEntry => ({
  runId: 'serve-bespoke-01',
  projectRoot: '/projects/serve',
  state: 'running',
  activity: 'live',
  objective: {name: 'throughput', unit: 'items/s', bestValue: 7200},
  costUsd: 4.25,
  needsUser: false,
  ...overrides,
});

const FAMILIES = ['Measurements', 'Agents', 'Workstreams', 'Correctness checks'];

function draw(
  delivery: RunDetailDelivery | undefined,
  retry: () => void = () => undefined,
  entry: GenericRunEntry = run(),
  freshness: 'current' | 'stale' = 'current',
) {
  return mount(
    <GenericRunScreen
      run={entry}
      freshness={freshness}
      delivery={delivery}
      retry={retry}
      trail={[{id: 'runs', label: 'Runs'}]}
    />,
  );
}

const headings = (screen: ReturnType<typeof draw>): string[] =>
  [...screen.container.querySelectorAll('h2')].map(heading => heading.textContent ?? '');

describe('the page of a generic run', () => {
  it('shows what the run is doing from its typed facts', () => {
    const screen = draw(genericRunDetailFixture('live'));
    expect(screen.container.querySelector('h1')?.textContent).toBe('serve');
    expect(headings(screen)).toEqual(FAMILIES);
    const text = screen.text();
    for (const words of [
      'Best so far',
      '7.2k',
      'items/s',
      'Bespoke serving systems critic',
      'Improve serving throughput',
      '93 of 100 trials completed',
      'Candidate correctness',
    ])
      expect(text).toContain(words);
    expect(text).not.toContain('Reading the run');
    // What measured a check is on its row's hover, not a column of its own.
    expect(
      screen.container.querySelector('[title*="Measured by: Candidate evaluation"]'),
    ).not.toBeNull();
    screen.unmount();
  });

  it('keeps every family in its frame in every state a detail can be in', () => {
    for (const fixture of GENERIC_RUN_DETAIL_FIXTURES) {
      const screen = draw(genericRunDetailFixture(fixture));
      expect(headings(screen), fixture).toEqual(FAMILIES);
      expect(screen.text(), fixture).not.toMatch(/undefined|NaN|null|\[object/);
      screen.unmount();
    }
  });

  it('says it is reading before the first answer, and again while it reconnects', () => {
    for (const delivery of [undefined, {_tag: 'Loading'} as const]) {
      const screen = draw(delivery);
      expect(screen.text()).toContain('Reading the run');
      expect(screen.text()).toContain('Spend');
      screen.unmount();
    }
    const stale = draw(genericRunDetailFixture('stale'));
    expect(stale.text()).toContain('Reconnecting');
    // The last safe read stays on screen while the link is down.
    expect(stale.text()).toContain('Improve serving throughput');
    stale.unmount();
  });

  it('offers a retry when the host refused the detail and holds nothing to show', () => {
    let retries = 0;
    const screen = draw(genericRunDetailFixture('refused'), () => {
      retries += 1;
    });
    expect(screen.text()).toContain('Run detail · Host too old');
    expect(screen.text()).toContain('Not read');
    expect(screen.text()).not.toContain('Reading the run');
    screen.press(screen.control('Retry run detail'));
    expect(retries).toBe(1);
    screen.unmount();
  });

  it('offers no retry while the detail is current', () => {
    const screen = draw(genericRunDetailFixture('live'));
    expect(screen.controls('Retry run detail')).toEqual([]);
    screen.unmount();
  });

  it('keeps the whole of a long name reachable, and states what was left out', () => {
    const long = draw(genericRunDetailFixture('long-name'));
    expect(
      long.container.querySelector('[title*="every interaction between them"]'),
    ).not.toBeNull();
    long.unmount();
    const truncated = draw(genericRunDetailFixture('truncated'));
    expect(truncated.text()).toContain('The oldest were left out to stay within the app’s limits');
    truncated.unmount();
  });

  it('says the run needs the person, and what to do, beside its figures', () => {
    const screen = draw(
      genericRunDetailFixture('live'),
      () => undefined,
      run({needsUser: true}),
      'stale',
    );
    expect(screen.text()).toContain('Input needed');
    expect(screen.text()).toContain('Run index last known');
    screen.unmount();
  });
});
