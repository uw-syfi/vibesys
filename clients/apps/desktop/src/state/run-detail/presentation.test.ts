import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {
  GENERIC_RUN_DETAIL_FIXTURES,
  genericRunDetailFixture,
  representativeRunDetailProjection,
} from '../../testing/run-detail/index.js';
import {presentRunDetail} from './index.js';

const projectionOf = (name: (typeof GENERIC_RUN_DETAIL_FIXTURES)[number]) => {
  const delivery = genericRunDetailFixture(name);
  if (delivery._tag === 'Loading' || delivery.projection === null) return null;
  return delivery.projection;
};

describe('the generic run detail presentation', () => {
  it('groups a representative run without losing provenance, lineage, usage, or dynamic roles', () => {
    const projection = representativeRunDetailProjection();
    const view = presentRunDetail(projection);

    expect(view.run).toMatchObject({
      id: 'serve-bespoke-01',
      lifecycle: {
        value: 'running',
        source: {kind: 'run-record', sequence: 1n, epoch: 1},
        scope: {kind: 'run', runId: 'serve-bespoke-01'},
      },
      freshness: {kind: 'current', throughSequence: 9n},
    });
    expect(view.agents).toMatchObject({
      source: {kind: 'run-log'},
      scope: {kind: 'run', runId: 'serve-bespoke-01'},
      freshness: {kind: 'current', throughSequence: 9n},
      items: [
        {
          role: 'bespoke-serving-systems-critic',
          model: 'model-x',
          provider: 'provider-y',
          providerSessionIds: ['session-open'],
          usage: {inputTokens: 11n, outputTokens: 7n, costUsd: 0.25},
          source: {kind: 'run-record', sequence: 6n, epoch: 1},
          scope: {kind: 'span', runId: 'serve-bespoke-01', spanId: 'agent-dynamic'},
        },
      ],
    });
    expect(view.workstreams.items[0]).toMatchObject({
      parentRevision: 'revision-base',
      links: [{rel: 'builds_on', to: 'revision-base'}],
      source: {sequence: 4n},
    });
    expect(view.correctness.items[0]).toMatchObject({
      frameworkSource: 'gpu',
      subject: 'candidate',
      source: {kind: 'run-record', sequence: 5n},
      scope: {kind: 'span', spanId: 'gate-throughput'},
    });
    expect(view.optimization.metrics.items).toMatchObject([
      {
        name: 'throughput',
        latest: {
          value: 7_200,
          source: {kind: 'run-record', sequence: 7n},
          scope: {kind: 'run', runId: 'serve-bespoke-01'},
        },
      },
      {
        name: 'gpu_utilization',
        role: 'resource',
        latest: {
          value: 75,
          source: {kind: 'run-record', sequence: 8n},
          scope: {kind: 'run', runId: 'serve-bespoke-01'},
        },
      },
    ]);
    expect(view.agents.items[0]?.usage?.cacheCreationTokens).toBeUndefined();
    expect(view.optimization.metrics.items[0]?.latest).toBe(
      view.optimization.metrics.items[0]?.points.at(-1),
    );
  });

  it('keeps empty, truncated, and unknown-descriptor states local to their fact families', () => {
    const empty = projectionOf('empty');
    const truncated = projectionOf('truncated');
    const unknown = projectionOf('unknown-descriptor');
    expect(empty && presentRunDetail(empty)).toMatchObject({
      run: {lifecycle: null},
      optimization: {
        objectives: {
          availability: {kind: 'unavailable', reason: 'typed-fact-not-recorded'},
        },
        metrics: {availability: {kind: 'available'}, items: []},
      },
      workstreams: {availability: {kind: 'available'}, items: []},
    });
    const truncatedView = truncated && presentRunDetail(truncated);
    expect(truncatedView).toMatchObject({
      optimization: {metrics: {availability: {kind: 'partial', reasons: ['state-truncated']}}},
      agents: {availability: {kind: 'partial', reasons: ['state-truncated']}},
      diagnostics: {truncation: {agentEvictions: 2, workstreamEvictions: 5}},
    });
    expect(truncatedView?.optimization.metrics.items[0]).toMatchObject({
      name: 'throughput',
      omittedPoints: 42,
    });
    expect(unknown && presentRunDetail(unknown)).toMatchObject({
      workstreams: {
        availability: {kind: 'partial', reasons: ['descriptor-unknown']},
        items: [],
      },
      diagnostics: {
        unprojectedSpans: {
          availability: {kind: 'available'},
          items: [{spanId: 'unknown-workstream', reason: 'descriptor-unknown'}],
        },
      },
    });
  });

  it('builds every acceptance fixture freshly and deterministically', () => {
    expect(GENERIC_RUN_DETAIL_FIXTURES).toEqual([
      'live',
      'finished',
      'stale',
      'refused',
      'truncated',
      'long-name',
      'empty',
      'unknown-descriptor',
      'dynamic-role',
    ]);
    for (const name of GENERIC_RUN_DETAIL_FIXTURES) {
      const first = genericRunDetailFixture(name);
      const second = genericRunDetailFixture(name);
      expect(first).toEqual(second);
      expect(first).not.toBe(second);
    }
    expect(genericRunDetailFixture('stale')).toMatchObject({
      _tag: 'Stale',
      why: 'disconnected',
      projection: {runId: 'fixture-stale'},
    });
    expect(genericRunDetailFixture('refused')).toMatchObject({
      _tag: 'Refused',
      fault: {kind: 'Unimplemented', retryable: false},
      projection: null,
    });
  });

  it('preserves every strategy-selected role string exactly', () => {
    fc.assert(
      fc.property(fc.string(), role => {
        const view = presentRunDetail(
          representativeRunDetailProjection({runId: 'run-role', dynamicRole: role}),
        );
        expect(view.agents.items[0]?.role).toBe(role);
      }),
    );
  });
});
