/**
 * Problems: what is wrong right now, as data with a stable identity, and the one board that turns
 * them into toasts. A screen's store derives its problems from its state (a pure function of
 * that state) and reports them under a source name; the board keeps one entry per identity, so a
 * repeat collapses; shows the newest first; drops a problem when no source reports it any more;
 * and remembers a dismissal only for as long as the problem lasts, so one that clears and comes
 * back shows again. The board is a data fold, a plain reducer with no mode and no effects.
 */
import {createStore, type Store} from '@vibesys/store';
import {BOARD_EMPTY, type BoardAction, type BoardState, boardStep, type Problem} from './board.js';

export {
  BOARD_EMPTY,
  type BoardAction,
  type BoardState,
  type BoardToast,
  boardStep,
  boardToasts,
  type Problem,
  type ProblemTone,
} from './board.js';

/** The app's one board of problems. */
export type ProblemBoard = Store<BoardState, BoardAction>;

export function createProblemBoard(): ProblemBoard {
  return createStore(boardStep, BOARD_EMPTY, () => {});
}

/** A store a source derives its problems from. */
interface Observable<S> {
  getState(): S;
  subscribe(listener: () => void): () => void;
}

/**
 * Report `derive(store state)` to `board` as `source` now and after every change of the store.
 * Stopping retracts everything the source reported.
 */
export function reportProblems<S>(
  board: ProblemBoard,
  source: string,
  store: Observable<S>,
  derive: (state: S) => readonly Problem[],
): () => void {
  const report = (): void =>
    board.dispatch({type: 'report', source, problems: derive(store.getState())});
  report();
  const stop = store.subscribe(report);
  return () => {
    stop();
    board.dispatch({type: 'report', source, problems: []});
  };
}
