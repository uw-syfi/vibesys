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
