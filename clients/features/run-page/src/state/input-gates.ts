/**
 * The gates that failed before the run's first idea began: the run could not measure its input.
 * They are plain `gate_finished` events, tied to no span (an idea's own gate is a span of its
 * workstream, shown with the idea), so without this reading a failed baseline shows nowhere: the
 * run has no idea failed and nothing measured, and says neither why.
 */
import type {RunEvent} from '@vibesys/contracts';

/** One gate that finished failed, as its event says. */
export interface GateFailure {
  /** The event's sequence: a view at an earlier sequence does not hold it yet. */
  readonly sequence: number;
  /** The gate's kind: `accuracy`, `benchmark`, `build`, `validation`. */
  readonly gate: string;
  readonly recipe: string | null;
  /** The end of what the gate printed, when it kept any. */
  readonly tail: string | null;
}

/** The gates that failed before any idea began, and whether one has (later failures are not these). */
export interface InputGates {
  readonly failures: readonly GateFailure[];
  readonly settled: boolean;
}

export const NO_INPUT_GATES: InputGates = {failures: [], settled: false};

/** The failure `event` reports, or null when it is no gate finishing failed. */
function gateFailureOf(event: RunEvent): GateFailure | null {
  const data = event.data;
  if (data?.kind !== 'gate_finished' || event.status !== 'failed') return null;
  return {
    sequence: event.sequence ?? 0,
    gate: data.gate,
    recipe: data.recipe ?? null,
    tail: data.output_tail ?? null,
  };
}

/** `gates` after `event`: a failure joins until an idea (a workstream span) begins, then it is settled. */
export function foldInputGates(gates: InputGates, event: RunEvent): InputGates {
  if (gates.settled) return gates;
  if (event.type === 'span_started') {
    const data = event.data;
    const kind =
      data !== null && data !== undefined && 'span' in data ? data.span.label?.kind : null;
    return kind === 'workstream' ? {...gates, settled: true} : gates;
  }
  const failure = gateFailureOf(event);
  // A replayed event is the same failure, not another.
  if (failure === null || gates.failures.some(known => known.sequence === failure.sequence)) {
    return gates;
  }
  return {...gates, failures: [...gates.failures, failure]};
}

/** The failures a view at sequence `at` holds (null: all of them). */
export function failuresAt(gates: InputGates, at: number | null): readonly GateFailure[] {
  return at === null ? gates.failures : gates.failures.filter(failure => failure.sequence <= at);
}

/** The failed gates as the screen lists them: one row, so a failed baseline takes the room of a line. */
export interface GateFailureView {
  /** The gates that failed, once each, in the order they did: `Accuracy · Benchmark`. */
  readonly label: string;
  /** Why, in a few words: the reason they share, else the first gate's. */
  readonly reason: string | null;
  /** What each printed, headed by its gate when more than one failed; null when none printed. */
  readonly tail: string | null;
}

const REASON_LIMIT = 90;
const ERROR_LINE = /\b([A-Z][A-Za-z0-9]*(?:Error|Exception)): ([^)\n]+)/g;

function clipped(text: string): string {
  return text.length <= REASON_LIMIT ? text : `${text.slice(0, REASON_LIMIT - 1)}\u2026`;
}

/** Why a gate failed in a few words: its last error, else the last line it printed; null with no output. */
export function gateReason(tail: string | null): string | null {
  if (tail === null) return null;
  const errors = [...tail.matchAll(ERROR_LINE)];
  const last = errors.at(-1);
  if (last !== undefined) return clipped(`${last[1]}: ${last[2]?.trim()}`);
  const line = tail
    .split('\n')
    .map(each => each.trim())
    .filter(each => each !== '')
    .at(-1);
  return line === undefined ? null : clipped(line);
}

function capitalized(text: string): string {
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
}

function nameOf(failure: GateFailure): string {
  return capitalized(
    failure.recipe === null ? failure.gate : `${failure.gate} \u00b7 ${failure.recipe}`,
  );
}

/** The failures as one row; null when none failed. */
export function gateFailureView(failures: readonly GateFailure[]): GateFailureView | null {
  if (failures.length === 0) return null;
  const names = [...new Set(failures.map(nameOf))];
  const reasons = failures.map(failure => gateReason(failure.tail));
  const shared = reasons.every(reason => reason === reasons[0]);
  const printed = failures.filter(failure => failure.tail !== null && failure.tail.trim() !== '');
  const tail =
    printed.length === 0
      ? null
      : printed
          .map(failure =>
            failures.length === 1
              ? (failure.tail ?? '').trimEnd()
              : `${nameOf(failure)}\n${(failure.tail ?? '').trimEnd()}`,
          )
          .join('\n\n');
  return {
    label: names.join(' \u00b7 '),
    reason: shared ? (reasons[0] ?? null) : (reasons.find(each => each !== null) ?? null),
    tail,
  };
}
