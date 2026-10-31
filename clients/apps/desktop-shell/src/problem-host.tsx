/**
 * The window's one host of problems: the stack of toasts over the app, drawn from the board every
 * screen reports to. Each window (the main one and a run window) has its own board and its own
 * host, mounted once at the window's root.
 */
import {type BoardState, boardToasts, type Problem, type ProblemBoard} from '@vibesys/problems';
import {ToastStack} from '@vibesys/ui';
import {type JSX, type ReactNode, useEffect, useMemo, useSyncExternalStore} from 'react';

function useBoard(board: ProblemBoard): BoardState {
  return useSyncExternalStore(board.subscribe, board.getState, board.getState);
}

/** `children`, with the board's toasts over them. */
export function ProblemHost(props: {
  readonly board: ProblemBoard;
  readonly children: ReactNode;
}): JSX.Element {
  const {board} = props;
  const state = useBoard(board);
  const toasts = useMemo(() => boardToasts(state), [state]);
  return (
    <>
      {props.children}
      <ToastStack
        label="Notifications"
        toasts={toasts}
        toggle={(id, open) => board.dispatch({type: 'toggle', id, open})}
        dismiss={id => board.dispatch({type: 'dismiss', id})}
      />
    </>
  );
}

/**
 * Report `problems` to `board` as `source` while the calling component is mounted: what it reports
 * follows the render, and leaving retracts it.
 */
export function useReportedProblems(
  board: ProblemBoard,
  source: string,
  problems: readonly Problem[],
): void {
  useEffect(() => {
    board.dispatch({type: 'report', source, problems});
  }, [board, source, problems]);
  useEffect(() => () => board.dispatch({type: 'report', source, problems: []}), [board, source]);
}
