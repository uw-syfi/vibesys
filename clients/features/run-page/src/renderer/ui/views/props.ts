/**
 * What every view of the Run screen receives: the run at the view's sequence (`RunModel`), the run
 * clock, and `zoom`, the one way a view moves the screen (open an idea, a step, an item, an agent,
 * switch the mode, go up). A view reads the run only through these props and the selectors in
 * `../../../state`; it never reads the route itself, so the frame (`../run-screen.tsx`) decides
 * which view shows where.
 *
 * One folder per view under `views/`, each owned on its own:
 * - `timeline/`: the Gantt over the run clock (run level) or over one idea's steps;
 * - `agents/`: the agent tree (run level, and compact beside an opened agent) and the agent pane;
 * - `logs/`: the logs of the run, an idea or a step;
 * - `step/`: an opened step (an agent turn as narrative, or a gate) and its item (a tool call);
 * - `idea/`: an opened idea's body (attempts, step strip, steps) and its compact step list.
 */
import type {CoreState, TokenLedger} from '@vibesys/run-state';
import type {ReactNode} from 'react';
import type {ModelNames, RunModel, WhoBySpan, ZoomEvent} from '../../../state/index.js';

/** Moves the screen: the route machine's events. */
export type Zoom = (event: ZoomEvent) => void;

/** The run clock (run seconds): where the run started and where it is now (null: unknown). */
interface RunClock {
  readonly start: number;
  readonly now: number | null;
}

/** What every view gets. */
export interface ViewBase {
  readonly emptyLabelsOnly?: boolean;
  readonly model: RunModel;
  readonly clock: RunClock;
  /** The tokens the run's turns spent, by turn span (empty before any turn finished). */
  readonly tokens: TokenLedger;
  /** The harness and model that ran each turn, by turn span, from the run's usage records. */
  readonly who: WhoBySpan;
  /** The names the host's catalogue gives the models; absent, or a model it lacks: the id. */
  readonly names?: ModelNames;
  readonly zoom: Zoom;
}

/** What a view that reads agent transcripts also gets: the run fold (null before it reached). */
export interface WithCore {
  readonly core: CoreState | null;
}

/**
 * What a view adds to the header of the pane the frame draws around it. `kicker` and `title`
 * replace the frame's; `meta` follows the frame's facts (such as a status chip) on the header's
 * line; `actions` come before the frame's own (such as the Logs switch). A view states its header
 * as a pure function of its props (`ViewHeader`), registered in `headers.ts`.
 */
export interface PaneHeader {
  readonly kicker?: string;
  readonly title?: string;
  readonly meta?: ReactNode;
  readonly actions?: ReactNode;
}

/** A view's header, from the same props as its body. */
export type ViewHeader<Props> = (props: Props) => PaneHeader;
