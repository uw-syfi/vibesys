import {describe, expect, test} from 'bun:test';
import fc from 'fast-check';
import {
  BOARD_EMPTY,
  type BoardAction,
  type BoardState,
  boardStep,
  boardToasts,
  createProblemBoard,
  type Problem,
  reportProblems,
} from './index.js';

const fold = (actions: readonly BoardAction[]): BoardState =>
  actions.reduce((state, action) => boardStep(state, action).state, BOARD_EMPTY);

const problem = (id: string, title = id, tone: Problem['tone'] = 'err'): Problem => ({
  id,
  tone,
  title,
});
const report = (source: string, ...problems: Problem[]): BoardAction => ({
  type: 'report',
  source,
  problems,
});
const idsOf = (state: BoardState): string[] => boardToasts(state).map(toast => toast.id);

const SOURCES = ['a', 'b', 'c'] as const;
const IDS = ['p', 'q', 'r', 's'] as const;
const PROBLEM = fc.record({
  id: fc.constantFrom(...IDS),
  tone: fc.constantFrom('warn', 'err'),
  title: fc.constantFrom('one', 'two'),
});
const ACTION: fc.Arbitrary<BoardAction> = fc.oneof(
  fc.record({
    type: fc.constant('report'),
    source: fc.constantFrom(...SOURCES),
    problems: fc.array(PROBLEM, {maxLength: 4}),
  }),
  fc.record({type: fc.constant('dismiss'), id: fc.constantFrom(...IDS)}),
  fc.record({type: fc.constant('toggle'), id: fc.constantFrom(...IDS), open: fc.boolean()}),
);

/**
 * The board as the spec words it, kept independently of the reducer: which identities are live,
 * when each first appeared, what the user dismissed and opened, and who reported last.
 */
class Reference {
  readonly #reports = new Map<string, Problem[]>();
  readonly #appeared = new Map<string, number>();
  readonly #dismissed = new Set<string>();
  readonly #open = new Set<string>();
  #clock = 0;

  apply(action: BoardAction): void {
    if (action.type === 'report') this.#report(action.source, action.problems);
    else if (this.live().has(action.id)) this.#touch(action);
  }

  #report(source: string, problems: readonly Problem[]): void {
    const unique = problems.filter(
      (each, index) => problems.findIndex(other => other.id === each.id) === index,
    );
    this.#reports.delete(source);
    if (unique.length > 0) this.#reports.set(source, unique);
    const live = this.live();
    for (const id of [...this.#appeared.keys()]) {
      if (live.has(id)) continue;
      this.#appeared.delete(id);
      this.#dismissed.delete(id);
      this.#open.delete(id);
    }
    for (const each of unique) {
      if (!this.#appeared.has(each.id)) this.#appeared.set(each.id, ++this.#clock);
    }
  }

  #touch(action: Exclude<BoardAction, {type: 'report'}>): void {
    if (action.type === 'dismiss') {
      this.#dismissed.add(action.id);
      this.#open.delete(action.id);
    } else if (action.open) this.#open.add(action.id);
    else this.#open.delete(action.id);
  }

  live(): Set<string> {
    return new Set([...this.#reports.values()].flatMap(each => each.map(item => item.id)));
  }

  shown(): {id: string; title: string; tone: Problem['tone']; open: boolean}[] {
    return [...this.#appeared]
      .filter(([id]) => !this.#dismissed.has(id))
      .sort(([, left], [, right]) => right - left)
      .map(([id]) => {
        const reporters = [...this.#reports.values()].reverse();
        const found = reporters.flat().find(each => each.id === id);
        if (found === undefined) throw new Error(`${id} is live but nobody reports it`);
        return {id, title: found.title, tone: found.tone, open: this.#open.has(id)};
      });
  }
}

describe('the board of problems', () => {
  test('property: any sequence gives one toast per live, undismissed identity, newest first', () => {
    fc.assert(
      fc.property(fc.array(ACTION, {maxLength: 40}), actions => {
        const reference = new Reference();
        let state = BOARD_EMPTY;
        for (const action of actions) {
          state = boardStep(state, action).state;
          reference.apply(action);
          const toasts = boardToasts(state);
          expect(new Set(toasts.map(toast => toast.id)).size).toBe(toasts.length);
          expect(toasts.map(({id, title, tone, open}) => ({id, title, tone, open}))).toEqual(
            reference.shown(),
          );
          for (const toast of toasts) expect(reference.live().has(toast.id)).toBe(true);
        }
      }),
      {numRuns: 300, seed: 1},
    );
  });

  test('property: a report that changes nothing returns the same state', () => {
    fc.assert(
      fc.property(
        fc.array(ACTION, {maxLength: 20}),
        fc.constantFrom(...SOURCES),
        (actions, source) => {
          const state = fold(actions);
          const current = state.reports.find(each => each.source === source)?.problems ?? [];
          expect(boardStep(state, {type: 'report', source, problems: current}).state).toBe(state);
        },
      ),
      {numRuns: 100, seed: 2},
    );
  });

  test('a repeat of one problem from two sources is one toast', () => {
    const state = fold([report('a', problem('x')), report('b', problem('x'))]);
    expect(idsOf(state)).toEqual(['x']);
  });

  test('the newest problem is on top', () => {
    const state = fold([report('a', problem('x')), report('b', problem('y'))]);
    expect(idsOf(state)).toEqual(['y', 'x']);
    expect(idsOf(fold([report('a', problem('x'), problem('y'))]))).toEqual(['y', 'x']);
  });

  test('a problem leaves when nothing reports it, and stays while another source does', () => {
    const both = fold([report('a', problem('x')), report('b', problem('x'))]);
    expect(idsOf(boardStep(both, report('a')).state)).toEqual(['x']);
    expect(idsOf(boardStep(boardStep(both, report('a')).state, report('b')).state)).toEqual([]);
  });

  test('a dismissed problem stays hidden while it lasts and shows again after it clears and recurs', () => {
    const shown = fold([report('a', problem('x', 'Could not save'))]);
    const dismissed = boardStep(shown, {type: 'dismiss', id: 'x'}).state;
    expect(idsOf(dismissed)).toEqual([]);
    const repeated = boardStep(dismissed, report('a', problem('x', 'Could not save again'))).state;
    expect(idsOf(repeated)).toEqual([]);
    const cleared = boardStep(repeated, report('a')).state;
    expect(idsOf(boardStep(cleared, report('a', problem('x'))).state)).toEqual(['x']);
  });

  test('a toast opens and closes, and is collapsed again when it recurs', () => {
    const shown = fold([report('a', problem('x'))]);
    expect(boardToasts(shown)[0]?.open).toBe(false);
    const open = boardStep(shown, {type: 'toggle', id: 'x', open: true}).state;
    expect(boardToasts(open)[0]?.open).toBe(true);
    const recurred = fold([
      report('a', problem('x')),
      {type: 'toggle', id: 'x', open: true},
      report('a'),
      report('a', problem('x')),
    ]);
    expect(boardToasts(recurred)[0]?.open).toBe(false);
  });

  test('reporting from a store follows it and retracts when stopped', () => {
    const board = createProblemBoard();
    let value = 0;
    const listeners = new Set<() => void>();
    const store = {
      getState: () => value,
      subscribe: (listener: () => void) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
    };
    const set = (next: number): void => {
      value = next;
      for (const listener of [...listeners]) listener();
    };
    const stop = reportProblems(board, 'screen', store, state =>
      state > 0 ? [problem('x', `failed ${state}`)] : [],
    );
    expect(boardToasts(board.getState())).toEqual([]);
    set(1);
    expect(boardToasts(board.getState()).map(toast => toast.title)).toEqual(['failed 1']);
    set(2);
    expect(boardToasts(board.getState()).map(toast => toast.title)).toEqual(['failed 2']);
    stop();
    expect(boardToasts(board.getState())).toEqual([]);
    expect(listeners.size).toBe(0);
  });
});
