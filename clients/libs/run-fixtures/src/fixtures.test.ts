import {describe, expect, it} from 'bun:test';
import {CandidateFold, type CandidateTree} from '@vibesys/run-state/candidates';
import {
  committedFixtures,
  FAILURE_REASONS,
  FIXTURE_NAMES,
  findLeaks,
  fixtureNamed,
  type RunFixture,
  splitLive,
  weeksRun,
} from './index.js';

function treeOf(fixture: RunFixture): CandidateTree {
  const fold = new CandidateFold();
  fold.apply(fixture.events);
  return fold.tree();
}

describe('the committed fixtures', () => {
  it('are named as the catalog says', () => {
    const names = committedFixtures().map(fixture => fixture.name);
    expect(new Set(names).size).toBe(names.length);
    expect([...names, 'weeks-600-ideas'].sort()).toEqual([...FIXTURE_NAMES].sort());
  });

  it('hold no host name, absolute path, user name, e-mail or address', () => {
    for (const fixture of [...committedFixtures(), weeksRun()]) {
      expect(findLeaks(fixture), fixture.name).toEqual([]);
    }
  });

  it('fold into candidates that each build on the input or an earlier candidate', () => {
    for (const fixture of [...committedFixtures(), weeksRun()]) {
      const seen = new Set<string>();
      for (const candidate of treeOf(fixture).candidates) {
        expect(candidate.parent === null || seen.has(candidate.parent), fixture.name).toBe(true);
        seen.add(candidate.id);
      }
    }
  });
});

describe('each scenario shows its case', () => {
  it('long titles: some are over 150 characters and one is a single unbroken token', () => {
    const titles = treeOf(fixtureNamed('long-titles')).candidates.map(c => c.title);
    expect(titles.some(title => title.length > 150)).toBe(true);
    expect(titles.some(title => title.length > 80 && !title.includes(' '))).toBe(true);
  });

  it('failure reasons: every reason the search spells names a failed idea', () => {
    const fixture = fixtureNamed('failure-reasons');
    const reasons = new Set(
      treeOf(fixture)
        .candidates.filter(candidate => candidate.outcome === 'failed')
        .map(candidate => candidate.reason),
    );
    for (const reason of FAILURE_REASONS) expect(reasons.has(reason), reason).toBe(true);
  });

  it('retries: an implementer turn tried up to five times, and one retrying now', () => {
    const spans = fixtureNamed('retries').events.flatMap(event => {
      const span = (event.data as {span?: {attempts?: unknown[]; status?: string}} | null)?.span;
      return span === undefined ? [] : [span];
    });
    expect(Math.max(...spans.map(span => span.attempts?.length ?? 0))).toBeGreaterThanOrEqual(5);
    expect(treeOf(fixtureNamed('retries')).candidates.some(c => c.outcome === 'running')).toBe(
      true,
    );
  });

  it('continuation rounds: rounds one to six, each starting from an earlier round', () => {
    const candidates = treeOf(fixtureNamed('continuation-rounds')).candidates;
    expect(candidates.map(candidate => candidate.round)).toEqual([1, 2, 3, 4, 5, 6]);
    for (const candidate of candidates) {
      expect(candidate.baseRound === null || candidate.baseRound < (candidate.round ?? 0)).toBe(
        true,
      );
    }
  });

  it('non-input bases: chains, a merge and a base that failed', () => {
    const candidates = treeOf(fixtureNamed('non-input-bases')).candidates;
    const byId = new Map(candidates.map(candidate => [candidate.id, candidate]));
    const built = candidates.filter(candidate => candidate.parent !== null);
    expect(built.length).toBeGreaterThanOrEqual(4);
    expect(built.some(candidate => byId.get(candidate.parent ?? '')?.outcome === 'failed')).toBe(
      true,
    );
  });

  it('live reconnect: the first batch, a live stretch and a rest that follows the drop', () => {
    const {first, beforeDrop, afterDrop} = splitLive(fixtureNamed('live-reconnect'));
    expect(first.length).toBeGreaterThan(0);
    expect(beforeDrop.length).toBeGreaterThan(0);
    expect(afterDrop.length).toBeGreaterThan(0);
  });

  it('finished run: ends, with its best adopted', () => {
    const fixture = fixtureNamed('finished-run');
    expect(fixture.state).toBe('finished');
    expect(fixture.events.at(-1)?.type).toBe('run_finished');
    const tree = treeOf(fixture);
    expect(tree.best?.adopted).toBe(true);
  });

  it('ten ideas: six passed and four failed, the best five steps from the input, tokens partly unknown', () => {
    const fixture = fixtureNamed('finished-ten-ideas');
    const tree = treeOf(fixture);
    expect(fixture.state).toBe('finished');
    expect(tree.candidates.filter(c => c.outcome === 'passed')).toHaveLength(6);
    expect(tree.candidates.filter(c => c.outcome === 'failed')).toHaveLength(4);
    expect(tree.bestPath).toHaveLength(5);
    const usage = fixture.events
      .filter(event => event.type === 'turn_usage_recorded')
      .map(event => event.data ?? {});
    // One turn reported no cache counts and one reported nothing.
    expect(usage).toHaveLength(9);
    expect(usage.some(each => !('cache_read_input_tokens' in each))).toBe(true);
  });

  it('two objectives: the overview declares both, and every measured gate reads both and the input', () => {
    const fixture = fixtureNamed('finished-two-objectives');
    const started = fixture.events.find(event => event.type === 'run_started');
    const overview = started?.data?.kind === 'run_started' ? started.data.overview : undefined;
    expect(overview?.objective.objectives).toEqual([
      {name: 'total_ops_per_sec', direction: 'max'},
      {name: 'p99_latency', direction: 'min'},
    ]);
    const tree = treeOf(fixture);
    const measured = tree.candidates.filter(c => c.measurement?.metricValue != null);
    expect(measured).toHaveLength(9);
    for (const idea of measured) {
      expect(idea.measurement?.readings.map(r => r.name)).toEqual([
        'total_ops_per_sec',
        'p99_latency',
      ]);
      expect(idea.measurement?.inputReadings.map(r => r.value)).toEqual([1_581_539, 12.4]);
    }
    expect(tree.candidates.filter(c => c.outcome === 'passed')).toHaveLength(8);
  });

  it('crashed after a turn: a recorded run whose idea, turn and round never ended, and whose baseline gates failed', () => {
    const fixture = fixtureNamed('crashed-after-turn');
    expect(fixture.state).toBe('finished');
    expect(fixture.events.at(-1)?.type).toBe('run_failed');
    const tree = treeOf(fixture);
    expect(tree.candidates.map(candidate => candidate.outcome)).toEqual(['running']);
    const failed = fixture.events.filter(
      event => event.type === 'gate_finished' && event.status === 'failed',
    );
    expect(failed).toHaveLength(2);
    // The reason runs past the registry's cut.
    expect((fixture.end?.reason ?? '').length).toBeGreaterThan(200);
  });

  it('14 days: 600 ideas, about 40 passed, three running, a long winning path', () => {
    const tree = treeOf(weeksRun());
    const passed = tree.candidates.filter(candidate => candidate.outcome === 'passed').length;
    expect(tree.candidates).toHaveLength(600);
    expect(passed).toBeGreaterThan(30);
    expect(passed).toBeLessThan(70);
    expect(tree.candidates.filter(candidate => candidate.outcome === 'running')).toHaveLength(3);
    expect(tree.bestPath.length).toBeGreaterThanOrEqual(10);
    const last = tree.candidates.at(-1);
    expect((last?.startedAt ?? 0) / 86_400).toBeGreaterThan(13);
  });

  it('14 days: is the same run every time', () => {
    expect(weeksRun()).toEqual(weeksRun());
  });
});
