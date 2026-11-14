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
    expect(html).toContain('2 need attention');
    expect(html).toContain('11 agents working');
    expect(html).toContain('$412');
    expect(html).toContain('live spend');
  });

  it('does not repeat the live count that the section header already carries', () => {
    const html = markup({...EMPTY, shown: 4, live: 4, agentsWorking: 2});
    expect(html).not.toContain('4 live');
  });

  it('omits the need-attention chip when nothing wants a look', () => {
    const html = markup({...EMPTY, shown: 3, live: 3, agentsWorking: 2});
    expect(html).toContain('2 agents working');
    expect(html).not.toContain('need attention');
  });

  it('leaves out a figure the hosts did not report rather than showing zero', () => {
    const html = markup({...EMPTY, shown: 2, live: 2, attention: 1});
    expect(html).toContain('need attention');
    expect(html).not.toContain('agents working');
    expect(html).not.toContain('live spend');
  });

  it('names a single working agent in the singular', () => {
    expect(markup({...EMPTY, shown: 1, live: 1, agentsWorking: 1})).toContain('1 agent working');
  });
});
