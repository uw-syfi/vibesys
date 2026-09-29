/** Shared view-model types: `derive.ts` and the view modules build them, `ui/` renders them. */

export type Connection = 'connecting' | 'connected' | 'disconnected' | 'error';

export interface ProsePart {
  kind: 'text' | 'code' | 'strong';
  text: string;
}

export type EndedWord = 'Completed' | 'Failed' | 'Interrupted';

export type RunControl =
  | {
      kind: 'action';
      action: 'pause' | 'resume';
      label: 'Pause' | 'Pausing' | 'Resume';
      tip: string;
      disabled: boolean;
    }
  | {
      kind: 'ended';
      word: EndedWord;
      /** A failed or interrupted run's one-line reason, shown in the title row. */
      summary: string | null;
      /** The full diagnostic text: the reason's hover hint. */
      tip: string | null;
    };

export interface Steers {
  /** Steers the backend journaled as pending and has not consumed yet. */
  pending: PendingSteer[];
  consumed: ConsumedSteer[];
}

export interface PendingSteer {
  /** `steer-<sequence of the pending control event>`. */
  id: string;
  text: string;
}

export interface ConsumedSteer extends PendingSteer {
  /** Sequence of the `control` consumed event: the steer's position in the log. */
  sequence: number;
  round: number | null;
  roundLabel: string | null;
  agentKind: string | null;
  /** The consuming call's execution, when the control event names it. */
  executionId: string | null;
}

/** One row of a rendered diff; `line` is the file line number when the patch gives one. */
export interface DiffLine {
  tone: 'add' | 'del' | 'ctx' | 'hunk' | 'meta';
  text: string;
  line: number | null;
}
