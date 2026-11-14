import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentRole,
  Harness,
  ModelCostSchema,
  RoleCostSchema,
  RunCostSchema,
  SpendBucketSchema,
  UsageSchema,
} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import fc from 'fast-check';
import {renderToStaticMarkup} from 'react-dom/server';
import type {CostView, FeedStatus, Freshness, HostStatusKind} from '../../state/index.js';
import {costViewArb} from '../../testing/orchestrator/cost-arbitrary.js';
import {
  ENDED_RUN,
  LONG_NAMES,
  NO_PRICES,
  NO_TURNS,
  PRICED_AND_UNPRICED,
} from '../../testing/orchestrator/cost-views.js';
import {FakeDaemon, generateScenario, hostHarness} from '../../testing/orchestrator/index.js';
import {COST_WORDS} from '../test-display-clock.js';
import {CostScreen} from './cost-screen.js';
import {formatCount} from './words.js';

const LIVE: FeedStatus = {_tag: 'Live'};
/** The host status a feed freshness is shown as. */
const HOST_KIND: Readonly<Record<Freshness, HostStatusKind>> = {
  live: 'live',
  'catching-up': 'catching-up',
  stale: 'stale',
  empty: 'waiting',
  unavailable: 'unavailable',
};
/** Names of the protocol that must never reach a person. */
const PROTOCOL = /UNSPECIFIED|AGENT_ROLE|undefined|NaN|\[object/;

function screen(
  view: CostView | undefined,
  options: {
    freshness?: Freshness;
    status?: FeedStatus;
    runOver?: boolean;
    retry?: () => void;
  } = {},
): string {
  return renderToStaticMarkup(
    <CostScreen
      view={view}
      status={{kind: HOST_KIND[options.freshness ?? 'live'], cause: null, staleForMs: null}}
      feed={options.status ?? LIVE}
      runOver={options.runOver ?? false}
      words={COST_WORDS}
      trail={[]}
      reading={null}
      retry={options.retry ?? (() => undefined)}
    />,
  );
}

/** What a person can read: the markup's text, one space between its pieces. */
const textOf = (html: string): string =>
  html
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

describe('the Cost screen over served views', () => {
  it('shows the spend against its cap, the rate, the split by type, by model and by kind', () => {
    const text = textOf(screen(PRICED_AND_UNPRICED));
    // The headline: a floor, because turns without a price are behind it.
    expect(text).toContain('≥ $318');
    expect(text).toContain('of $500');
    expect(text).toContain('$14/h');
    expect(text).toContain('1,433');
    // Unpriced turns are a count beside the floor that explains it, with no price made up for them.
    expect(text).toMatch(/≥ \$318[\s\S]*41 unpriced/);
    // By agent type, with agent counts and turns, never one row per agent.
    for (const word of ['Worker', '×17', '774', '$205', 'Lead', '12 unpriced', 'Profiler']) {
      expect(text).toContain(word);
    }
    expect(text).toMatch(/Profiler No price/);
    // By actual CLI provider and model, as the finished turns reported them.
    expect(text).toContain('Claude Code · model-a');
    expect(text).toContain('$206');
    // By kind, from the host's own totals.
    for (const word of ['Cache read', '212.4M', 'Reasoning', '5.2M', 'Cache write', '9.6M']) {
      expect(text).toContain(word);
    }
    expect(text).not.toMatch(PROTOCOL);
  });

  it('draws the spend as bars by model on a dollar axis, one bar for each bucket the host sent', () => {
    const html = screen(PRICED_AND_UNPRICED);
    expect(html.match(/class="vs-bars-bar"/g)).toHaveLength(
      PRICED_AND_UNPRICED.series.buckets.length,
    );
    expect(html).toContain('Spend over time, in dollars');
    expect(html.match(/class="vs-bars-tick"/g)?.length).toBeGreaterThanOrEqual(2);
    // The legend names each model once, as the rows below do.
    expect(html).toContain('aria-label="Models"');
  });

  it('shows no price as a dash and a note, never as a zero, when none was ever reported', () => {
    const html = screen(NO_PRICES);
    const text = textOf(html);
    expect(text).toContain('—');
    expect(text).toContain('No price');
    // The cap is the only dollar figure: no price is made up, and no scale is drawn for none.
    expect(text.replace('of $50.00', '')).not.toMatch(/\$\d/);
    expect(html).not.toContain('vs-bars');
    expect(text).toContain('of $50.00');
    expect(html).not.toContain('class="vs-meter"');
    expect(text).toMatch(/Unpriced turns\s*9/);
  });

  it('draws the meter against the cap when a price is known, and none when the run has no cap', () => {
    expect(screen(PRICED_AND_UNPRICED)).toContain('class="vs-meter"');
    const uncapped = screen({...PRICED_AND_UNPRICED, maxCostUsd: null});
    expect(uncapped).not.toContain('class="vs-meter"');
    expect(textOf(uncapped)).not.toContain(' of $');
  });

  it('says there are no turns yet, and splits nothing, for a run with no finished turn', () => {
    const html = screen(NO_TURNS);
    expect(textOf(html)).toContain('No turns yet.');
    expect(html).not.toContain('vs-bars');
    expect(html).not.toContain('vs-bar-rows');
  });

  it('says Live of a running run on a live host, and nothing of an ended one', () => {
    expect(textOf(screen(ENDED_RUN, {runOver: true}))).not.toContain('Live');
    expect(textOf(screen(PRICED_AND_UNPRICED))).toContain('Live');
    expect(textOf(screen(ENDED_RUN, {runOver: true}))).not.toContain('≥');
  });

  it('shows the spend rate of the last hour only while the run is spending', () => {
    for (const view of [PRICED_AND_UNPRICED, ENDED_RUN]) {
      expect(textOf(screen(view))).toContain('Last hour');
      expect(textOf(screen(view, {runOver: true}))).not.toContain('Last hour');
      expect(textOf(screen(view, {runOver: true}))).not.toContain('/h');
    }
  });

  it('says why a figure is a floor, and only then', () => {
    fc.assert(
      fc.property(costViewArb, view => {
        const text = textOf(screen(view));
        const floor = view.total.costUsd !== null && view.unpricedTurns > 0;
        expect(text.includes('≥')).toBe(floor);
        if (floor) expect(text).toContain(`${formatCount(view.unpricedTurns)} unpriced`);
      }),
      {numRuns: 40},
    );
  });

  it('says how current the view is in its header, whichever way the stream stands', () => {
    const words: readonly (readonly [Freshness, string])[] = [
      ['catching-up', 'Catching up'],
      ['stale', 'Stale'],
      ['empty', 'Waiting'],
      ['unavailable', 'Unavailable'],
    ];
    for (const [freshness, word] of words) {
      expect(textOf(screen(PRICED_AND_UNPRICED, {freshness}))).toContain(word);
      expect(textOf(screen(undefined, {freshness}))).toContain(word);
    }
  });

  it('shows an old host as unavailable and nothing else, with no retry that cannot work', () => {
    const status: FeedStatus = {
      _tag: 'Failed',
      fault: {kind: 'Unimplemented', detail: 'no such method', retryable: false},
    };
    const html = screen(undefined, {freshness: 'unavailable', status});
    const text = textOf(html);
    expect(text).toContain('Unavailable');
    expect(text).toContain('too old');
    expect(html).not.toContain('vs-button');
    expect(html).not.toContain('vs-card');
  });

  it('shows a failure inline beside the status, with Retry when trying again can work', () => {
    const status: FeedStatus = {
      _tag: 'Failed',
      fault: {kind: 'DeadlineExceeded', detail: 'deadline', retryable: true},
    };
    const html = screen(PRICED_AND_UNPRICED, {freshness: 'stale', status});
    // The last view stays on screen, marked stale, with the reason and the way to retry beside it.
    expect(textOf(html)).toContain('Stale');
    expect(textOf(html)).toContain('Timed out');
    expect(textOf(html)).toContain('Retry');
    expect(html).toContain('≥\u2005$318');
    // It is a line in the screen, not a full-width banner.
    expect(html).not.toMatch(/banner/i);
  });

  it('keeps every name whole for the pointer and lets the layout cut it', () => {
    const html = screen(LONG_NAMES);
    const longest = LONG_NAMES.models[0]?.model.model ?? '';
    expect(longest.length).toBeGreaterThan(60);
    expect(html).toContain(`title="Claude Code · ${longest}"`);
    // A provider/model pair is an identifier: it is drawn in the code face.
    expect(html).toContain('data-mono="true"');
  });

  it('reads any view the contract allows without a word of the protocol, a NaN or a missing value', () => {
    fc.assert(
      fc.property(costViewArb, view => {
        for (const freshness of ['live', 'stale'] as const) {
          expect(textOf(screen(view, {freshness}))).not.toMatch(PROTOCOL);
        }
      }),
      {numRuns: 40},
    );
  });

  it('says when provider and model attribution was truncated without weakening the totals', () => {
    const text = textOf(screen({...PRICED_AND_UNPRICED, modelsTruncated: true}));
    expect(text).toContain('Some provider and model rows are not shown');
    expect(text).toContain('≥ $318');
  });
});

describe('the Cost screen over the stream', () => {
  const scenario = generateScenario({seed: 3, subagents: 1, live: false});
  const usage = (fields: Parameters<typeof create<typeof UsageSchema>>[1]) =>
    create(UsageSchema, fields);

  it.effect('shows the figures the host sent, as they arrive through the fold', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      const at = Date.parse('2026-11-01T19:00:00Z');
      daemon.publishCost(
        create(RunCostSchema, {
          runId: scenario.runId,
          sequence: 5n,
          asOf: timestampFromMs(at),
          total: usage({costUsd: 12.5, outputTokens: 900n}),
          maxCostUsd: 40,
          turns: 4,
          unpricedTurns: 1,
          roles: [
            create(RoleCostSchema, {
              role: AgentRole.LEAD,
              agents: 1,
              turns: 4,
              unpricedTurns: 1,
              usage: usage({costUsd: 12.5, outputTokens: 900n}),
              totalTokens: 123_400n,
            }),
          ],
          models: [
            create(ModelCostSchema, {
              model: {harness: Harness.CODEX, model: 'model-a'},
              turns: 4,
              unpricedTurns: 1,
              usage: usage({costUsd: 12.5}),
              totalTokens: 7_650_000n,
            }),
          ],
          series: {
            start: timestampFromMs(at - 3_600_000),
            width: {seconds: 3600n, nanos: 0},
            buckets: [
              create(SpendBucketSchema, {
                start: timestampFromMs(at - 3_600_000),
                costUsd: 12.5,
                byModel: [{model: {harness: Harness.CODEX, model: 'model-a'}, costUsd: 12.5}],
              }),
            ],
          },
          lastHourUsd: 12.5,
        }),
      );
      const h = yield* hostHarness(daemon);
      h.mount(h.session.costs.watch(scenario.runId));
      yield* h.elapse(1);
      const view = (yield* h.model).data.costs[scenario.runId];
      const freshness = yield* h.read(h.session.costs.freshness(scenario.runId));
      const status = yield* h.read(h.session.costs.status(scenario.runId));
      const text = textOf(screen(view, {freshness, status}));
      expect(text).toContain('≥ $12.50');
      expect(text).toContain('of $40.00');
      expect(text).toContain('$12.50/h');
      expect(text).toContain('Live');
      expect(text).toContain('123.4k');
      expect(text).toContain('7.7M');
      expect(text).toMatch(/≥ \$12\.50[\s\S]*1 unpriced/);
    }),
  );
});
