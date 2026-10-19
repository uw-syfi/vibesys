/**
 * The gates that failed before the run's first idea began: the run could not measure its input.
 * They are plain `gate_finished` events, tied to no span (an idea's own gate is a span of its
 * workstream, shown with the idea), so without this reading a failed baseline shows nowhere: the
 * run has no idea failed and nothing measured, and says neither why.
 *
 * A gate that failed may be tried again, even after the first idea began (the input's measurement
 * can be retried while ideas run): a later attempt of the same gate (and recipe) that passes
 * supersedes the failure, which stays as history and no longer reads as the gate's standing. A
 * failure after the first idea began is that idea's own and joins nothing; a pass still counts, as
 * it shows the gate works.
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
  /**
   * The sequence of the first later attempt of the same gate (and recipe) that passed, or null
   * while none has: the failure is history then, not the gate's standing.
   */
  readonly supersededBy: number | null;
}

/** The gates that failed before any idea began, and whether one has (later failures are not these). */
export interface InputGates {
  readonly failures: readonly GateFailure[];
  /**
   * Every pass seen, with its sequence. Whether a failure stands is read from these, not from the
   * order the page learned of the two: a failure brought by a summary after a pass arrived live is
   * still superseded by it.
   */
  readonly passes: readonly GatePass[];
  readonly settled: boolean;
}

export const NO_INPUT_GATES: InputGates = {failures: [], passes: [], settled: false};

/** The failure `event` reports, or null when it is no gate finishing failed. */
function gateFailureOf(event: RunEvent): GateFailure | null {
  const data = event.data;
  if (data?.kind !== 'gate_finished' || event.status !== 'failed') return null;
  return {
    sequence: event.sequence ?? 0,
    gate: data.gate,
    recipe: data.recipe ?? null,
    tail: data.output_tail ?? null,
    supersededBy: null,
  };
}

export interface GatePass {
  readonly sequence: number;
  readonly gate: string;
  readonly recipe: string | null;
}

/** The gate `event` reports passing, or null when it is no gate finishing without failing. */
function gatePassOf(event: RunEvent): GatePass | null {
  const data = event.data;
  if (data?.kind !== 'gate_finished' || event.status === 'failed') return null;
  return {sequence: event.sequence ?? 0, gate: data.gate, recipe: data.recipe ?? null};
}

function sameGate(failure: GateFailure, other: GatePass): boolean {
  return failure.gate === other.gate && failure.recipe === other.recipe;
}

/** `failures` each with the first pass of its gate after it as the one that superseded it. */
function resolved(
  failures: readonly GateFailure[],
  passes: readonly GatePass[],
): readonly GateFailure[] {
  let changed = false;
  const next = failures.map(failure => {
    const first = passes
      .filter(pass => pass.sequence > failure.sequence && sameGate(failure, pass))
      .reduce<number | null>(
        (least, pass) => (least === null || pass.sequence < least ? pass.sequence : least),
        null,
      );
    if (first === failure.supersededBy) return failure;
    changed = true;
    return {...failure, supersededBy: first};
  });
  return changed ? next : failures;
}

/**
 * `gates` with `failures` and `passes` joined to what it holds, each once, in sequence order, and
 * every failure's superseding pass read again. Joining is order-independent: a page that learns of
 * a failure from a summary after it saw the pass live reads the same gates as one that saw both live.
 */
export function joinedInputGates(
  gates: InputGates,
  failures: readonly GateFailure[],
  passes: readonly GatePass[],
): InputGates {
  const knownFailures = new Set(gates.failures.map(failure => failure.sequence));
  const newFailures = failures.filter(failure => !knownFailures.has(failure.sequence));
  const knownPasses = new Set(gates.passes.map(pass => pass.sequence));
  const newPasses = [
    ...new Map(
      passes.filter(pass => !knownPasses.has(pass.sequence)).map(pass => [pass.sequence, pass]),
    ).values(),
  ];
  const allPasses =
    newPasses.length === 0
      ? gates.passes
      : [...gates.passes, ...newPasses].sort((a, b) => a.sequence - b.sequence);
  const joined =
    newFailures.length === 0
      ? gates.failures
      : [...gates.failures, ...newFailures].sort((a, b) => a.sequence - b.sequence);
  const settled = resolved(joined, allPasses);
  if (settled === gates.failures && allPasses === gates.passes) return gates;
  return {...gates, failures: settled, passes: allPasses};
}

/**
 * `gates` after `event`: a failure joins until an idea (a workstream span) begins, then it is
 * settled; a pass of the same gate supersedes the failures before it.
 */
export function foldInputGates(gates: InputGates, event: RunEvent): InputGates {
  if (event.type === 'span_started') {
    if (gates.settled) return gates;
    const data = event.data;
    const kind =
      data !== null && data !== undefined && 'span' in data ? data.span.label?.kind : null;
    return kind === 'workstream' ? {...gates, settled: true} : gates;
  }
  const pass = gatePassOf(event);
  if (pass !== null) return joinedInputGates(gates, [], [pass]);
  const failure = gateFailureOf(event);
  // Once an idea has begun, a failure is the idea's own: only passes still count.
  if (failure === null || gates.settled) return gates;
  // A replayed event is the same failure, not another.
  return joinedInputGates(gates, [failure], []);
}

/** The failures a view at sequence `at` holds (null: all of them), as they stood then. */
export function failuresAt(gates: InputGates, at: number | null): readonly GateFailure[] {
  if (at === null) return gates.failures;
  return gates.failures
    .filter(failure => failure.sequence <= at)
    .map(failure =>
      failure.supersededBy !== null && failure.supersededBy > at
        ? {...failure, supersededBy: null}
        : failure,
    );
}

/** The failed gates as the screen lists them: one row, so a failed baseline takes the room of a line. */
export interface GateFailureView {
  /** The gates that failed, once each, in the order they did: `Accuracy · Benchmark`. */
  readonly label: string;
  /** Why, in words: the reason they share, else the first gate's, whole. */
  readonly reason: string | null;
  /** What each printed, headed by its gate when more than one failed; null when none printed. */
  readonly tail: string | null;
  /**
   * How many attempts failed: of the gate tried most often for a failure that stands (2 when a gate
   * was tried again and failed again), in all for failures a later attempt passed over.
   */
  readonly attempts: number;
}

/** Gates that failed and then passed on a later attempt: their failed attempts as history. */
export type GateHistoryView = GateFailureView;

const ERROR_LINE = /\b([A-Z][A-Za-z0-9]*(?:Error|Exception)): ([^)\n]+)/g;

const SOME_VALUE = /\bSome\(([^()]*)\)/g;

/**
 * `text` with a Rust optional's debug spelling read as its value: a benchmark written in Rust
 * prints `prompt_tokens=Some(8192)` for a value it holds. The gate's own output is the tool's, not
 * ours to change, so the sentence the screen words from it says `prompt_tokens=8192`; the printed
 * output behind the disclosure stays as it was.
 */
function withoutOptionals(text: string): string {
  let current = text;
  for (let next = current.replace(SOME_VALUE, '$1'); next !== current; ) {
    current = next;
    next = current.replace(SOME_VALUE, '$1');
  }
  return current;
}

/** Why a gate failed in words: its last error, else the last line it printed; null with no output. */
export function gateReason(tail: string | null): string | null {
  if (tail === null) return null;
  const errors = [...tail.matchAll(ERROR_LINE)];
  const last = errors.at(-1);
  if (last !== undefined) return withoutOptionals(`${last[1]}: ${last[2]?.trim()}`);
  const line = tail
    .split('\n')
    .map(each => each.trim())
    .filter(each => each !== '')
    .at(-1);
  return line === undefined ? null : withoutOptionals(line);
}

function capitalized(text: string): string {
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
}

function nameOf(failure: GateFailure): string {
  return capitalized(
    failure.recipe === null ? failure.gate : `${failure.gate} · ${failure.recipe}`,
  );
}

function viewOf(failures: readonly GateFailure[]): GateFailureView {
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
    label: names.join(' · '),
    reason: shared ? (reasons[0] ?? null) : (reasons.find(each => each !== null) ?? null),
    tail,
    attempts: failures.length,
  };
}

/** The gates still failed (no later attempt passed) as one row; null when none stands failed. */
export function gateFailureView(failures: readonly GateFailure[]): GateFailureView | null {
  const standing = failures.filter(failure => failure.supersededBy === null);
  if (standing.length === 0) return null;
  const tries = new Map<string, number>();
  for (const failure of standing) tries.set(nameOf(failure), (tries.get(nameOf(failure)) ?? 0) + 1);
  return {...viewOf(standing), attempts: Math.max(...tries.values())};
}

/** The failed attempts a later attempt of the same gate passed over, as one row; null when none. */
export function gateHistoryView(failures: readonly GateFailure[]): GateHistoryView | null {
  const history = failures.filter(failure => failure.supersededBy !== null);
  return history.length === 0 ? null : viewOf(history);
}
