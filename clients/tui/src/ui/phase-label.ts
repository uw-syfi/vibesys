/**
 * Turns a backend `round_label` into something an operator can read.
 *
 * The labels are loop-internal identifiers that exist for round attribution,
 * not for display: `round-1-retry-2-implementer`, `gen-2-cand-1-mutator`,
 * `impl issue #7 att2`. `roundNumberFromLabel` in core-state parses them and
 * the run map keys off them, so they are load-bearing and stay exactly as they
 * are. This is presentation only.
 *
 * Every producer, so the parser covers all three loop modes:
 *
 *   agent   src/vibesys/loops/agent/loop.py
 *           round-N-pre, round-N-plan, round-N-profiler, round-N,
 *           round-N-retry-R-implementer, round-N-retry-R-judge,
 *           round-N-retry-R-single-agent, round-N-retry-R
 *   evolve  src/vibesys/loops/evolve/loop.py
 *           gen-G-cand-C-mutator, gen-G-cand-C-judge, gen-G-cand-C-profiler
 *   plain   src/vibesys/loops/plain/loop.py
 *           impl issue #ID attN, judge issue #ID attN, perf_eval iter N
 */

/** What an agent is doing, in the operator's words rather than the loop's. */
export interface PhaseDescription {
  /** The activity: `implementing`, `judging`, `planning`. Never an identifier. */
  activity: string;
  /** Retry or attempt number, when the label carries one and it is past the first. */
  attempt: number | null;
  /** What is being worked on: `candidate 1`, `issue #7`. Null for plain rounds. */
  subject: string | null;
}

/**
 * Stage word per label suffix. The suffix is the only part of a label that says
 * what is happening; the numbers around it say which round it belongs to, which
 * the rounds strip already shows.
 */
const STAGE_WORDS: Readonly<Record<string, string>> = {
  pre: 'preparing',
  plan: 'planning',
  profiler: 'profiling',
  implementer: 'implementing',
  judge: 'judging',
  mutator: 'mutating',
  'single-agent': 'working',
};

/**
 * Agent kinds, for when the label carries no stage of its own.
 *
 * Every kind the backend runs an agent as, which is also every kind
 * `expectedRoles` in `run-map.ts` seeds as a selectable phase: an entry missing
 * here is a backend identifier on screen.
 */
const KIND_WORDS: Readonly<Record<string, string>> = {
  orchestrator: 'planning',
  implementer: 'implementing',
  judge: 'judging',
  profiler: 'profiling',
  perf_eval: 'measuring',
  mutator: 'mutating',
  chat: 'answering',
};

const AGENT_ROUND = /^round-(\d+)(?:-retry-(\d+))?(?:-(.+))?$/;
const EVOLVE_CANDIDATE = /^gen-(\d+)-cand-(\d+)-(.+)$/;
const PLAIN_ISSUE = /^(impl|judge)\s+issue\s+#(\S+)\s+att(\d+)$/;
const PLAIN_PERF = /^perf_eval\s+iter\s+(.+)$/;

interface PhaseContext {
  label: string;
  kind: string;
}

type PhaseParser = (context: PhaseContext) => PhaseDescription | null;

function parseChat({label, kind}: PhaseContext): PhaseDescription | null {
  // The chat runs beside the loop rather than inside it, and labels itself in
  // words already. Both spellings appear in recorded runs.
  if (label !== 'experiment chat' && label !== 'experiment-chat' && kind !== 'chat') return null;
  return {activity: 'answering', attempt: null, subject: null};
}

function parseEvolveCandidate({label, kind}: PhaseContext): PhaseDescription | null {
  const match = EVOLVE_CANDIDATE.exec(label);
  if (match === null) return null;
  const [, , candidate, stage] = match;
  return {
    activity: STAGE_WORDS[stage ?? ''] ?? fallbackActivity(stage, kind),
    attempt: null,
    subject: candidate === undefined ? null : `candidate ${candidate}`,
  };
}

function parsePlainIssue({label}: PhaseContext): PhaseDescription | null {
  const match = PLAIN_ISSUE.exec(label);
  if (match === null) return null;
  const [, stage, id, attempt] = match;
  return {
    activity: stage === 'judge' ? 'judging' : 'implementing',
    attempt: attemptOrNull(attempt),
    subject: id === undefined ? null : `issue #${id}`,
  };
}

function parsePlainPerformance({label}: PhaseContext): PhaseDescription | null {
  return PLAIN_PERF.test(label) ? {activity: 'measuring', attempt: null, subject: null} : null;
}

function parseAgentRound({label, kind}: PhaseContext): PhaseDescription | null {
  const match = AGENT_ROUND.exec(label);
  if (match === null) return null;
  const [, , retry, stage] = match;
  return {
    // `round-N` and `round-N-retry-R` carry no stage; the kind names the
    // agent that is between stages.
    activity:
      stage === undefined
        ? fallbackActivity(null, kind)
        : (STAGE_WORDS[stage] ?? fallbackActivity(stage, kind)),
    attempt: attemptOrNull(retry),
    subject: null,
  };
}

/** Label families in precedence order. Chat kind intentionally overrides every label. */
const PHASE_PARSERS: readonly PhaseParser[] = [
  parseChat,
  parseEvolveCandidate,
  parsePlainIssue,
  parsePlainPerformance,
  parseAgentRound,
];

/**
 * Describes what is running, from the round label and the agent kind.
 *
 * The label wins where the two disagree: it distinguishes the stages one kind
 * covers (an orchestrator plans in `round-N-plan` and prepares in
 * `round-N-pre`), which the kind alone cannot.
 */

export function describePhase(
  roundLabel: string | null,
  agentKind: string | null,
): PhaseDescription | null {
  const label = roundLabel?.trim() ?? '';
  const kind = agentKind?.trim() ?? '';
  if (label === '' && kind === '') return null;

  const context = {label, kind};
  for (const parse of PHASE_PARSERS) {
    const phase = parse(context);
    if (phase !== null) return phase;
  }

  // An unrecognized label is a loop we do not know about. Falling back to the
  // kind keeps an identifier off the screen; the label stays reachable in the
  // Agents pane and the round strip.
  return {activity: fallbackActivity(null, kind), attempt: null, subject: null};
}

/**
 * The word for an agent kind on its own, or null when this file has none.
 *
 * Same table as `describePhase`'s fallback, and deliberately not the same
 * contract. A phase description has to say something about what is running, so
 * an unrecognized kind degrades to the kind itself; a note about which agent a
 * view is filtered to has no such obligation, and printing the raw kind there
 * is the backend identifier #517 exists to keep out of the header. Callers that
 * can drop the text get null and drop it.
 */
export function agentKindText(agentKind: string | null): string | null {
  return KIND_WORDS[agentKind?.trim() ?? ''] ?? null;
}

/** One line for the header: `implementing · attempt 2`, `mutating candidate 1`. */
export function phaseText(phase: PhaseDescription | null): string | null {
  if (phase === null) return null;
  const subject = phase.subject === null ? phase.activity : `${phase.activity} ${phase.subject}`;
  return phase.attempt === null ? subject : `${subject} · attempt ${phase.attempt}`;
}

/**
 * First attempts are the normal case and saying so adds nothing; a retry is
 * the thing worth surfacing.
 */
function attemptOrNull(value: string | undefined): number | null {
  if (value === undefined) return null;
  const attempt = Number(value);
  return Number.isFinite(attempt) && attempt > 1 ? attempt : null;
}

function fallbackActivity(stage: string | undefined | null, kind: string): string {
  if (stage !== undefined && stage !== null) {
    const known = STAGE_WORDS[stage];
    if (known !== undefined) return known;
  }
  return KIND_WORDS[kind] ?? (kind === '' ? 'working' : kind);
}
