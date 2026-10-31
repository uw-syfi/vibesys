import {only, type Step} from '@vibesys/store';

/** How a problem reads: `warn` is something to know, `err` something that failed. */
export type ProblemTone = 'warn' | 'err';

/**
 * Something that is wrong right now, as data. `id` is its stable identity: the same problem has the
 * same `id` on every report, from any screen, so repeats collapse into one and a problem that goes
 * away and comes back is recognised as the same one. `detail` is the longer account behind `title`.
 */
export interface Problem {
  readonly id: string;
  readonly tone: ProblemTone;
  readonly title: string;
  readonly detail?: string;
}

/** What a source reported at its last report, in the order it listed it (oldest first). */
interface Report {
  readonly source: string;
  readonly problems: readonly Problem[];
}

/**
 * The board of problems. `reports` holds each reporting source's current problems, the most recent
 * reporter last. `order` is the live identities in the order they first appeared, oldest first;
 * `dismissed` and `open` are the identities the user dismissed or expanded. Every identity in
 * `order`, `dismissed` and `open` is live: one that is no longer reported is dropped from all
 * three, so a problem that clears and recurs starts again as new, shown and collapsed.
 */
export interface BoardState {
  readonly reports: readonly Report[];
  readonly order: readonly string[];
  readonly dismissed: readonly string[];
  readonly open: readonly string[];
}

export type BoardAction =
  /** `source` now reports exactly `problems`; none retracts everything it reported. */
  | {readonly type: 'report'; readonly source: string; readonly problems: readonly Problem[]}
  | {readonly type: 'dismiss'; readonly id: string}
  | {readonly type: 'toggle'; readonly id: string; readonly open: boolean};

export const BOARD_EMPTY: BoardState = {reports: [], order: [], dismissed: [], open: []};

/** What the board shows for one problem. */
export interface BoardToast {
  readonly id: string;
  readonly tone: ProblemTone;
  readonly title: string;
  readonly detail?: string;
  readonly open: boolean;
}

function sameProblems(left: readonly Problem[], right: readonly Problem[]): boolean {
  return (
    left.length === right.length &&
    left.every((problem, index) => {
      const other = right[index];
      return (
        other !== undefined &&
        problem.id === other.id &&
        problem.tone === other.tone &&
        problem.title === other.title &&
        problem.detail === other.detail
      );
    })
  );
}

function liveIds(reports: readonly Report[]): ReadonlySet<string> {
  return new Set(reports.flatMap(report => report.problems.map(problem => problem.id)));
}

/** Drop identities that are no longer live; keep `list` itself when none is dropped. */
function whileLive(list: readonly string[], live: ReadonlySet<string>): readonly string[] {
  return list.every(id => live.has(id)) ? list : list.filter(id => live.has(id));
}

function report(state: BoardState, source: string, problems: readonly Problem[]): BoardState {
  // A source lists each identity once; a repeat within one report is the same problem.
  const unique = problems.filter(
    (problem, index) => problems.findIndex(other => other.id === problem.id) === index,
  );
  const current = state.reports.find(each => each.source === source);
  if (current === undefined ? unique.length === 0 : sameProblems(current.problems, unique)) {
    return state;
  }
  const others = state.reports.filter(each => each.source !== source);
  const reports = unique.length === 0 ? others : [...others, {source, problems: unique}];
  const live = liveIds(reports);
  const kept = whileLive(state.order, live);
  const known = new Set(kept);
  const appeared = [...live].filter(id => !known.has(id));
  // Within a report the listed order is the order of appearance: its last problem is the newest.
  const listed = reports.flatMap(each => each.problems.map(problem => problem.id));
  const added = [...new Set(listed)].filter(id => appeared.includes(id));
  return {
    reports,
    order: added.length === 0 ? kept : [...kept, ...added],
    dismissed: whileLive(state.dismissed, live),
    open: whileLive(state.open, live),
  };
}

/** The board's one transition: reports replace, dismissal and expansion are the user's. */
export function boardStep(state: BoardState, action: BoardAction): Step<BoardState, never> {
  switch (action.type) {
    case 'report':
      return only(report(state, action.source, action.problems));
    case 'dismiss':
      return only(
        !state.order.includes(action.id) || state.dismissed.includes(action.id)
          ? state
          : {
              ...state,
              dismissed: [...state.dismissed, action.id],
              open: state.open.filter(id => id !== action.id),
            },
      );
    case 'toggle': {
      if (!state.order.includes(action.id) || state.open.includes(action.id) === action.open) {
        return only(state);
      }
      return only({
        ...state,
        open: action.open ? [...state.open, action.id] : state.open.filter(id => id !== action.id),
      });
    }
  }
}

/**
 * The toasts to show, newest first: one per live, undismissed identity. A problem several sources
 * report reads as the most recent reporter wrote it.
 */
export function boardToasts(state: BoardState): readonly BoardToast[] {
  const dismissed = new Set(state.dismissed);
  const open = new Set(state.open);
  const toasts: BoardToast[] = [];
  for (const id of [...state.order].reverse()) {
    if (dismissed.has(id)) continue;
    const problem = [...state.reports]
      .reverse()
      .flatMap(each => each.problems)
      .find(each => each.id === id);
    if (problem === undefined) continue;
    toasts.push({
      id,
      tone: problem.tone,
      title: problem.title,
      ...(problem.detail === undefined ? {} : {detail: problem.detail}),
      open: open.has(id),
    });
  }
  return toasts;
}
