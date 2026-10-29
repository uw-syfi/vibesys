import {describe, expect, test} from 'bun:test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {ideaBases} from './idea-bases.js';
import {effectWords, ideaEffects} from './idea-effects.js';
import {IDEAS_FILTER_DEFAULT, ideasView} from './ideas-view.js';
import {EMPTY_TRACE_LOG, traceStep} from './live.js';
import {createRunReader} from './run-model.js';

/**
 * The live run of `bases-and-effects`: seven ideas whose bases are the best, a passed idea that is
 * no longer the best, the input and their own work; the framework's verdict on each effect; and
 * the best named while the run works.
 */
const EVENTS = fixtureNamed('bases-and-effects').events;
const MODEL = createRunReader().read(
  traceStep(EMPTY_TRACE_LOG, {kind: 'events', events: EVENTS}),
  null,
);
const CORE = reduceEventBatch(initialCoreState(), EVENTS);

const titled = (title: string) => {
  const found = MODEL.candidates.candidates.find(item => item.title === title);
  if (found === undefined) throw new Error(`no idea ${title}`);
  return found;
};

describe('where an idea started from', () => {
  const bases = ideaBases(MODEL.candidates, MODEL.direction);
  const labelOf = (title: string) => bases.get(titled(title).id)?.label;

  test('the best when it started, even after a better idea passed', () => {
    expect(labelOf('Batched enqueue on the padded counters')).toBe('best');
    // Idea 2 led when idea 4 started, so idea 4's base (idea 1) is only a passed idea.
    expect(labelOf('Per-core free lists')).toBe('a passed idea');
  });

  test('the input, and its own work for a continuation', () => {
    expect(labelOf('Padded counters')).toBe('input');
    expect(labelOf('Spin before parking')).toBe('input');
    expect(labelOf('Prefetch the next slot')).toBe('own work');
  });

  test('the rows carry the same base', () => {
    const rows = ideasView(MODEL, IDEAS_FILTER_DEFAULT).rows;
    for (const row of rows) expect(row.trail).toEqual(bases.get(row.id) ?? row.trail);
  });
});

describe('the best while the run works', () => {
  test('exactly one idea reads best, the leader, and none is adopted', () => {
    const rows = ideasView(MODEL, IDEAS_FILTER_DEFAULT).rows;
    const marked = rows.filter(row => row.badges.length > 0 && row.badges[0]?.label === 'best');
    expect(marked.map(row => row.title)).toEqual(['Prefetch the next slot']);
    expect(rows.some(row => row.badges.some(badge => badge.label === 'adopted'))).toBe(false);
  });
});

describe('the framework verdict on an idea’s effect', () => {
  test('supported, within noise and disproven come from the measurement comparisons', () => {
    const effects = ideaEffects(CORE, MODEL.candidates);
    const said = (title: string) => effects.get(titled(title).id);
    expect(said('Padded counters')).toBe('supported');
    expect(said('Spin before parking')).toBe('within noise');
    expect(said('Per-core free lists')).toBe('disproven');
  });

  test('an idea nothing compared has none, and a run with no fold has none at all', () => {
    expect(ideaEffects(CORE, MODEL.candidates).has(titled('Shard the free list').id)).toBe(false);
    expect(ideaEffects(null, MODEL.candidates).size).toBe(0);
  });

  test('a failed idea that measured nothing has none, though its revision is a measured idea’s', () => {
    // It kept its base's code, whose revision has a comparison of its own.
    const failed = titled('Widen the batch to sixty-four');
    const kept = failed.attempts.at(-1)?.revision;
    expect(kept).toBeDefined();
    expect(CORE.comparisons[kept ?? '']).toBeDefined();
    expect(ideaEffects(CORE, MODEL.candidates).has(failed.id)).toBe(false);
  });

  test('a running idea is measuring while its comparison takes in runs, then has its verdict', () => {
    const running = titled('Combine adjacent slots');
    expect(running.outcome).toBe('running');
    expect(ideaEffects(CORE, MODEL.candidates).get(running.id)).toBe('measuring');
  });

  test('the words say the verdict is against the idea’s base', () => {
    expect(effectWords('within noise')).toBe('within noise of its base');
    expect(effectWords('supported')).toBe('beat its base');
    expect(effectWords('disproven')).toBe('did not beat its base');
    expect(effectWords('measuring')).toBe('measuring');
  });

  test('a later comparison of the same commit supersedes an earlier one', () => {
    const first = EVENTS.find(event => event.data?.kind === 'measurement_compared');
    if (first?.data?.kind !== 'measurement_compared') throw new Error('fixture has none');
    const later = {
      ...first,
      sequence: (EVENTS.at(-1)?.sequence ?? 0) + 1,
      data: {...first.data, verdict: 'worse' as const},
    };
    const core = reduceEventBatch(initialCoreState(), [...EVENTS, later]);
    expect(ideaEffects(core, MODEL.candidates).get(titled('Padded counters').id)).toBe('disproven');
  });
});
