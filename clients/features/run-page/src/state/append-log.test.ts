import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {AppendLog} from './append-log.js';

interface View {
  readonly log: AppendLog<number>;
  readonly items: readonly number[];
}

/** A few increasing numbers after `items`, like the sequences a run's log holds. */
function after(random: Seeded, items: readonly number[]): number[] {
  const added: number[] = [];
  let last = items.at(-1) ?? 0;
  for (let n = random.int(4); n > 0; n -= 1) {
    last += 1 + random.int(3);
    added.push(last);
  }
  return added;
}

/** Views appended mostly from the newest (following a run), sometimes from an older one. */
function grow(random: Seeded): View[] {
  const views: View[] = [{log: AppendLog.of<number>([]), items: []}];
  for (let step = 0; step < 60; step += 1) {
    const source = views[random.bool() ? views.length - 1 : random.int(views.length)];
    if (source === undefined) continue;
    const added = after(random, source.items);
    views.push({log: source.log.append(added), items: [...source.items, ...added]});
  }
  return views;
}

describe('an append log', () => {
  test('every view keeps its own items, whichever view is appended to', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      for (const {log, items} of grow(random)) {
        expect([...log]).toEqual([...items]);
        expect(log.length).toBe(items.length);
        expect(log.at(-1)).toBe(items.at(-1));
        const cut = random.int(200);
        expect(log.countWhile(item => item < cut)).toBe(items.filter(item => item < cut).length);
      }
    }
  });

  test('finds the last item of a sorted prefix by halving, and none before the first', () => {
    const log = AppendLog.of([1, 3, 5]).append([7]);
    expect(log.lastWhile(item => item <= 4)).toBe(3);
    expect(log.lastWhile(item => item <= 0)).toBeUndefined();
    expect(log.lastWhile(item => item <= 9)).toBe(7);
  });
});
