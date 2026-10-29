/**
 * An ended or live run's summary (`runs.summary`) as the Run screen takes it: everything the run
 * level shows, from a read model the host keeps beside the run's journal, so the screen opens
 * without reading the journal.
 *
 * The summary is the client's own fold of the journal to a sequence (`through`), so seeding the
 * screen from it is not a second definition of the run level:
 * - the run trace and overview are the host's snapshot of the run, taken through the trace's own
 *   `snapshot` step (`traceStep`), which a live session's snapshot takes too;
 * - the turns' last replies and the run's spend are folded into the core by the same reducer the
 *   journal's events are, from events rebuilt from the summary, so `implementerOutcomes` and the
 *   header's spend read what they read for the whole journal.
 *
 * The rebuilt events are a seed, not history: they carry sequences of their own (1, 2, ...) and no
 * time, and the transcript of the run (which the summary does not hold) replaces the core when it
 * is read (`RunHistory`).
 */
import type {RunEvent, RunSnapshot, RunStatus} from '@vibesys/contracts';
import {validateRunEvent, validateRunSnapshot} from '@vibesys/contracts';
import type {RunTerminal, UsageRecord} from '@vibesys/contracts/host-helper';

/** A count of tokens as a summary words it; `null`: not reported. */
export interface WireTokens {
  readonly input: number | null;
  readonly cacheRead: number | null;
  readonly cacheWrite: number | null;
  readonly output: number | null;
  readonly reasoning: number | null;
}

/**
 * Whether a journal records the run trace its ideas and measurements are read from. `predates_trace`:
 * a VibeSys from before the trace wrote it, so the run's spans are missing, not empty.
 */
type TraceFormat = 'recorded' | 'predates_trace';

/** A run's summary as the host's reader returns it (`RunSummary` of `@vibesys/ipc`), pages joined. */
export interface WireSummary {
  readonly throughSequence: number;
  readonly eventCount: number;
  readonly journalBytes: number;
  readonly trace: TraceFormat;
  readonly roundsFinished: number;
  readonly overview: unknown;
  readonly spans: readonly unknown[];
  readonly replies: readonly {
    readonly spanId: string;
    readonly kind: string;
    readonly jsonSchema: unknown;
    readonly value: unknown;
  }[];
  readonly tokens: WireTokens;
  readonly usage: readonly {readonly spanId: string; readonly tokens: WireTokens}[];
  /** One record per finished turn: their tokens sum to `tokens`. */
  readonly records: readonly UsageRecord[];
  /** The gates that finished before the first idea began, in journal order. */
  readonly inputGates: readonly WireGate[];
  /** The terminal event that ended the journal, when one did. */
  readonly terminal: WireTerminal | null;
}

/** One input gate as a summary words it (`SummaryGate` of `@vibesys/ipc`). */
export interface WireGate {
  readonly sequence: number;
  readonly gate: string;
  readonly recipe: string | null;
  readonly passed: boolean;
  readonly outputTail: string | null;
}

/** The terminal event as a summary words it (`SummaryTerminal` of `@vibesys/ipc`). */
export interface WireTerminal {
  readonly sequence: number;
  readonly kind: RunTerminal['kind'];
  readonly message: string | null;
}

/** What the screen takes from a summary: the host's snapshot of the run, and the seed events. */
export interface SummaryFacts {
  /** The run's spans and overview through `through`, validated. */
  readonly snapshot: RunSnapshot;
  /** The events that fold a turn's reply and the run's spend into the core. */
  readonly seed: readonly RunEvent[];
  /** The journal's last sequence the summary covers. */
  readonly through: number;
  readonly eventCount: number;
  readonly journalBytes: number;
  readonly trace: TraceFormat;
  readonly roundsFinished: number;
  /** The run's usage records through `through`. */
  readonly records: readonly UsageRecord[];
  /**
   * The input gates as the `gate_finished` events they were, at their own journal sequences: the
   * events `foldInputGates` reads, so the page folds them with the reducer a journal's events take.
   */
  readonly gates: readonly RunEvent[];
  /** How the journal ended, when it did: the run's whole failure message lives here. */
  readonly terminal: WireTerminal | null;
}

/** What one summary read settled to. */
export type SummaryReply =
  | ({readonly kind: 'summary'} & SummaryFacts)
  | {readonly kind: 'unsupported'}
  | {readonly kind: 'gone'}
  | {readonly kind: 'failed'; readonly message: string};

/**
 * The summary read of a run: not asked yet, awaited under a token, taken (what it covers), set
 * aside for a live session's data, not served by this host (an older helper: the screen reads the
 * whole history instead), or failed.
 */
export type RunSummaryState =
  | {readonly kind: 'idle'}
  | {readonly kind: 'reading'; readonly token: number}
  | {
      readonly kind: 'ready';
      readonly through: number;
      readonly eventCount: number;
      readonly journalBytes: number;
      readonly trace: TraceFormat;
      readonly roundsFinished: number;
      readonly records: readonly UsageRecord[];
      readonly terminal: WireTerminal | null;
    }
  /** It arrived after a live session had already folded the run; the session's data stands. */
  | {readonly kind: 'superseded'}
  | {readonly kind: 'unsupported'}
  /** The host holds no record of the run: there is nothing to read, and asking again finds none. */
  | {readonly kind: 'gone'}
  | {readonly kind: 'failed'; readonly message: string};

/** The time of every rebuilt event: they carry none, and nothing reads it. */
const SEED_TIME = '1970-01-01T00:00:00Z';

type Seeded = Pick<RunEvent, 'sequence' | 'type' | 'span_id'> & {readonly data: object};

function seedEvent(runId: string, event: Seeded): RunEvent {
  return validateRunEvent({
    protocol_version: 1,
    run_id: runId,
    timestamp: SEED_TIME,
    text: '',
    ...event,
  });
}

function tokenFields(tokens: WireTokens): Record<string, number | null> {
  return {
    input_tokens: tokens.input,
    cache_read_input_tokens: tokens.cacheRead,
    cache_creation_input_tokens: tokens.cacheWrite,
    output_tokens: tokens.output,
    reasoning_output_tokens: tokens.reasoning,
  };
}

/**
 * What every turn spent beyond the turns a span names: the run's total less the named turns' sum,
 * count by count. A count nobody reported stays unreported, and one the named turns account for
 * entirely needs no event; null when nothing is left to say.
 */
function unnamedSpend(summary: WireSummary): WireTokens | null {
  const sum = (pick: (tokens: WireTokens) => number | null): number | null => {
    const counts = summary.usage.map(entry => pick(entry.tokens));
    return counts.every(count => count === null)
      ? null
      : counts.reduce<number>((total, count) => total + (count ?? 0), 0);
  };
  const rest = (pick: (tokens: WireTokens) => number | null): number | null => {
    const total = pick(summary.tokens);
    if (total === null) return null;
    const named = sum(pick);
    const left = total - (named ?? 0);
    // A reported zero with no named report is still a report, not an unknown count.
    return left > 0 || (left === 0 && named === null) ? left : null;
  };
  const left: WireTokens = {
    input: rest(tokens => tokens.input),
    cacheRead: rest(tokens => tokens.cacheRead),
    cacheWrite: rest(tokens => tokens.cacheWrite),
    output: rest(tokens => tokens.output),
    reasoning: rest(tokens => tokens.reasoning),
  };
  return Object.values(left).every(count => count === null) ? null : left;
}

/**
 * The `gate_finished` events `summary`'s input gates were. Each keeps its journal sequence, so a
 * view of the run at an earlier sequence holds the gates it would hold from the journal.
 */
function gateEvents(summary: WireSummary, runId: string): readonly RunEvent[] {
  return summary.inputGates.map(gate =>
    validateRunEvent({
      protocol_version: 1,
      run_id: runId,
      timestamp: SEED_TIME,
      text: '',
      sequence: gate.sequence,
      type: 'gate_finished',
      status: gate.passed ? 'completed' : 'failed',
      data: {
        kind: 'gate_finished',
        gate: gate.gate,
        recipe: gate.recipe,
        output_tail: gate.outputTail,
      },
    }),
  );
}

/** The events that fold `summary`'s replies and spend into a core, in a sequence of their own. */
function seedEvents(summary: WireSummary, runId: string): readonly RunEvent[] {
  const events: RunEvent[] = [];
  const next = (): number => events.length + 1;
  for (const reply of summary.replies) {
    events.push(
      seedEvent(runId, {
        sequence: next(),
        type: 'agent_execution_finished',
        span_id: reply.spanId,
        data: {
          kind: 'agent_execution_finished',
          result: '',
          reply: {kind: reply.kind, json_schema: reply.jsonSchema, value: reply.value},
        },
      }),
    );
  }
  for (const entry of summary.usage) {
    events.push(
      seedEvent(runId, {
        sequence: next(),
        type: 'turn_usage_recorded',
        span_id: entry.spanId,
        data: {kind: 'turn_usage_recorded', span_id: entry.spanId, ...tokenFields(entry.tokens)},
      }),
    );
  }
  const unnamed = unnamedSpend(summary);
  if (unnamed !== null) {
    events.push(
      seedEvent(runId, {
        sequence: next(),
        type: 'turn_usage_recorded',
        span_id: null,
        data: {kind: 'turn_usage_recorded', ...tokenFields(unnamed)},
      }),
    );
  }
  return events;
}

/**
 * The facts a summary gives the screen for run `runId`, validated. `status` is the run's, which a
 * snapshot names and the trace does not read. Throws naming what is not a valid snapshot or event.
 */
export function summaryFacts(summary: WireSummary, runId: string, status: RunStatus): SummaryFacts {
  const snapshot = validateRunSnapshot({
    run_id: runId,
    sequence: summary.throughSequence,
    status,
    spans: summary.spans,
    overview: summary.overview,
  });
  return {
    snapshot,
    seed: seedEvents(summary, runId),
    through: summary.throughSequence,
    eventCount: summary.eventCount,
    journalBytes: summary.journalBytes,
    trace: summary.trace,
    roundsFinished: summary.roundsFinished,
    records: summary.records,
    gates: gateEvents(summary, runId),
    terminal: summary.terminal,
  };
}
