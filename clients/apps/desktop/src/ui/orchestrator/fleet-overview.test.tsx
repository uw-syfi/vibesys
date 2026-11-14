import {describe, expect, it} from '@effect/vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import type {FleetOverview} from '../../state/index.js';
import {FleetOverviewBar} from './fleet-overview.js';

const EMPTY: FleetOverview = {
  shown: 0,
  live: 0,
  ended: 0,
  attention: 0,
  agentsWorking: null,
  liveCostUsd: null,
};

const markup = (overview: FleetOverview): string =>
  renderToStaticMarkup(<FleetOverviewBar overview={overview} />);

describe('FleetOverviewBar', () => {
  it('shows nothing when there is nothing live to summarize', () => {
    expect(markup(EMPTY)).toBe('');
  });

  it('shows nothing when live runs report no attention, no agents and no spend', () => {
    expect(markup({...EMPTY, shown: 3, live: 3})).toBe('');
  });

  it('reads the need-attention count, the agents working and the live spend', () => {
    const html = markup({
      shown: 6,
      live: 4,
      ended: 2,
      attention: 2,
      agentsWorking: 11,
      liveCostUsd: 412,
    });
    expect(html).toContain('Need attention');
    expect(html).toContain('of 4 live');
    expect(html).toContain('Agents working');
    expect(html).toContain('>11<');
    expect(html).toContain('$412');
    expect(html).toContain('Live spend');
  });

  it('says nothing waits on the operator, in the quiet tone, when no run wants a look', () => {
    const html = markup({...EMPTY, shown: 3, live: 3, agentsWorking: 2});
    expect(html).toContain('Nothing is waiting on you');
    expect(html).not.toContain('data-tone="warn"');
  });

  it('draws the attention count in the problem tone only when it is above zero', () => {
    const html = markup({...EMPTY, shown: 2, live: 2, attention: 1});
    expect(html).toContain('of 2 live');
    expect(html).toContain('data-tone="warn"');
  });

  it('leaves out a figure the hosts did not report rather than showing zero', () => {
    const html = markup({...EMPTY, shown: 2, live: 2, attention: 1});
    expect(html).not.toContain('Agents working');
    expect(html).not.toContain('Live spend');
  });

  it('names a single working agent in the singular', () => {
    expect(markup({...EMPTY, shown: 1, live: 1, agentsWorking: 1})).toContain(
      'agent across live runs',
    );
  });
});
