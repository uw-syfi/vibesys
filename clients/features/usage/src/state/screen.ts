/** What the Usage page renders and what it can do: the types its store and its screen share. */
import type {UsageView} from './usage-view.js';
import type {UsageWindowChoice} from './window.js';

/** What the page is doing about its figures. */
export type UsagePhase = 'loading' | 'ready' | 'unsupported' | 'failed';

/** What the screen renders. */
export interface UsageScreenState {
  readonly window: UsageWindowChoice;
  readonly phase: UsagePhase;
  /** The figures for the window: null while the first are read, and when there are none to show. */
  readonly view: UsageView | null;
  /** Why the last question failed, for a pointer to rest on. */
  readonly problem: string | null;
}

export interface UsageActions {
  readonly choose: (window: UsageWindowChoice) => void;
  readonly refresh: () => void;
}
