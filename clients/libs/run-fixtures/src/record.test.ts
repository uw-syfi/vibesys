import {describe, expect, it} from 'bun:test';
import {findLeaks, fixtureNamed, journalEvents, parseFixture, recordFixture} from './index.js';

const POLICY = {users: ['alice'], hosts: ['build-host']};
const OBJECTIVE = {text: 'Go faster.', metric: 'ops', unit: 'ops/s', direction: 'max'} as const;

/** A journal whose events say where, on which host and as whom the run was made. */
function journal(): string {
  const base = {protocol_version: 1, run_id: '20261015-163047', type: 'run_configured'};
  const events = [
    {
      ...base,
      sequence: 1,
      timestamp: '2026-10-15T16:30:47+00:00',
      data: {
        kind: 'run_configured',
        run_log_path: '/home/alice/projects/queue-rs/.vibesys/logs/20261015-163047',
        project_root: '/home/alice/projects/queue-rs',
        model: 'example-model',
        objective: 'Run on ssh:build-host as alice@build-host (10.1.2.3)',
        search_policy: 'beam',
        benchmark_contract: true,
        pareto_objectives: null,
        max_continuation_rounds: null,
        max_unmeasured_turns: null,
        source: 'loop',
      },
    },
    {
      ...base,
      sequence: 2,
      type: 'agent_output_chunk',
      timestamp: '2026-10-15T16:31:00+00:00',
      data: {
        kind: 'agent_output_chunk',
        channel: 'assistant',
        content: 'Edited /Users/alice/work/queue-rs/src/lib.rs; see https://git.corp.internal/q',
      },
    },
  ];
  return events.map(event => JSON.stringify(event)).join('\n');
}

const request = {
  name: 'recorded',
  description: 'A run recorded on build-host by alice.',
  journal: journal(),
  state: 'live',
  objective: OBJECTIVE,
  policy: POLICY,
} as const;

describe('recordFixture', () => {
  it('writes a fixture that holds no host, path or user name from the journal', () => {
    const fixture = recordFixture(request);
    const text = JSON.stringify(fixture);
    for (const secret of [
      'alice',
      'build-host',
      '/home/',
      '/Users/',
      '10.1.2.3',
      'corp.internal',
    ]) {
      expect(text, secret).not.toContain(secret);
    }
    expect(findLeaks(fixture, POLICY)).toEqual([]);
    expect(fixture.events.map(event => event.sequence)).toEqual([1, 2]);
    expect(fixture.description).toBe('A run recorded on remote-host by user.');
  });

  it('reads a JSON Lines journal and an array of the same events alike', () => {
    const lines = journalEvents(request.journal);
    expect(recordFixture({...request, journal: JSON.stringify(lines)})).toEqual(
      recordFixture(request),
    );
  });

  it('records a fixture that parses back to itself', () => {
    const fixture = recordFixture(request);
    expect(parseFixture(JSON.parse(JSON.stringify(fixture)))).toEqual(fixture);
  });

  it('names the journal line that is not JSON, and the event the protocol rejects', () => {
    expect(() => recordFixture({...request, journal: `${journal()}\nnot json`})).toThrow(
      'journal line 3',
    );
    expect(() =>
      recordFixture({...request, journal: JSON.stringify([{sequence: 1, type: 'run_started'}])}),
    ).toThrow();
    expect(() => recordFixture({...request, journal: ''})).toThrow('no events');
  });
});

describe('parseFixture', () => {
  const good = JSON.parse(JSON.stringify(fixtureNamed('finished-run'))) as Record<string, unknown>;

  it('accepts a committed fixture', () => {
    expect(parseFixture(good).name).toBe('finished-run');
  });

  it('names the key or path it refuses', () => {
    const cases: readonly (readonly [Record<string, unknown>, string])[] = [
      [{...good, extra: 1}, 'fixture.extra: unknown key'],
      [{...good, format: 2}, 'fixture.format'],
      [{...good, name: 'Not Kebab'}, 'fixture.name'],
      [{...good, state: 'paused'}, 'fixture.state'],
      [{...good, end: undefined}, 'fixture.end'],
      [{...good, liveAfter: 3}, 'finished run has no live stream'],
      [{...good, events: []}, 'fixture.events'],
      [{...good, objective: {...(good['objective'] as object), direction: 'up'}}, 'direction'],
      [{...good, events: [...(good['events'] as unknown[]).slice(0, 2).reverse()]}, 'sequence'],
    ];
    for (const [value, message] of cases) {
      expect(() => parseFixture(value), message).toThrow(message);
    }
  });

  it('refuses a live run that ends, and a stream that drops before it starts', () => {
    const live = JSON.parse(JSON.stringify(fixtureNamed('live-reconnect'))) as Record<
      string,
      unknown
    >;
    expect(() => parseFixture({...live, end: {outcome: 'completed', reason: 'done'}})).toThrow(
      'a live run has not ended',
    );
    expect(() => parseFixture({...live, liveAfter: 20, dropAfter: 10})).toThrow('drops before');
  });
});
