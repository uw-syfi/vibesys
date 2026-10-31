/**
 * Reading a run's spans for the Overview and Work tabs: a span's label attributes, its nearest
 * labelled workstream, its descendants, and the run-clock time the spans have reached. Kinds the
 * dynamic search declares (`planning_call`, `workstream`, `agent_turn`, `gate`) are read by name;
 * anything else is read plainly from the span's name, title and status.
 */
import type {Span} from '@vibesys/contracts';

/** The run's spans in start order, with the lookups the tabs need. */
export interface SpanIndex {
  readonly spans: readonly Span[];
  readonly byId: ReadonlyMap<string, Span>;
  readonly children: ReadonlyMap<string, readonly Span[]>;
}

export function indexSpans(spans: readonly Span[]): SpanIndex {
  const byId = new Map<string, Span>();
  const children = new Map<string, Span[]>();
  for (const span of spans) {
    byId.set(span.id, span);
    if (span.parent === null) continue;
    const siblings = children.get(span.parent);
    if (siblings === undefined) children.set(span.parent, [span]);
    else siblings.push(span);
  }
  return {spans, byId, children};
}

/** The span's label kind, or null when its search did not label it. */
function kindOf(span: Span): string | null {
  return span.label?.kind ?? null;
}

/**
 * What the run's own steps are called, by the registered name of the operation or flow that
 * records them. A search labels the steps it cares about (its workstreams, turns and gates); the
 * rest are the plumbing around them, named by a closed registry (an operation's kind, a flow's
 * name) and worded here, never in the backend. A strategy's operations are
 * `<strategy>.<operation>`, so a name not listed whole is looked up by its operation
 * (`multi.render_role_artifacts` reads as `render_role_artifacts`).
 */
const STEP_NAMES: Readonly<Record<string, string>> = {
  render_role_artifacts: 'Writing the agent’s instructions',
  interpret_evidence: 'Reading the measurement',
  capture_command: 'Profiling',
  validate_local: 'Validating the candidate',
  verify_parent_revision: 'Checking the starting revision',
  export_patch: 'Exporting the candidate’s changes',
  write_handoff: 'Writing the hand-off notes',
  'roadmap.read': 'Reading the roadmap',
  'roadmap.put_items': 'Updating the roadmap',
  'issue_board.read': 'Reading the issue board',
  'issue_board.apply_changes': 'Updating the issue board',
  'issue_board.log_progress': 'Logging progress',
  'agent.turn': 'Agent turn',
  play_round: 'Play round',
  'measure-input': 'Measuring the input',
};

/** The words for a step's registered name, or null when the registry does not list it. */
export function stepName(name: string): string | null {
  const whole = STEP_NAMES[name];
  if (whole !== undefined) return whole;
  return name.includes('.') ? (STEP_NAMES[name.slice(name.indexOf('.') + 1)] ?? null) : null;
}

/** The label's attributes as a record; empty when it has none or they do not parse to one. */
export function attrsOf(span: Span): Readonly<Record<string, unknown>> {
  const text = span.label?.attrs_json;
  if (text === undefined || text === null || text === '') return {};
  try {
    const parsed: unknown = JSON.parse(text);
    return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    // Attributes are display data here: a malformed one shows as absent, never breaks the tab.
    return {};
  }
}

export function stringAttr(span: Span, key: string): string | null {
  const value = attrsOf(span)[key];
  return typeof value === 'string' && value !== '' ? value : null;
}

export function numberAttr(span: Span, key: string): number | null {
  const value = attrsOf(span)[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** The nearest ancestor (or the span itself) labelled `kind`. */
export function nearest(index: SpanIndex, span: Span, kind: string): Span | null {
  const seen = new Set<string>();
  let at: Span | undefined = span;
  while (at !== undefined && !seen.has(at.id)) {
    if (kindOf(at) === kind) return at;
    seen.add(at.id);
    at = at.parent === null ? undefined : index.byId.get(at.parent);
  }
  return null;
}

/** The latest run-clock time any span records, or null with no spans. */
export function latestTime(spans: readonly Span[]): number | null {
  let latest: number | null = null;
  const see = (value: number | null | undefined): void => {
    if (value === null || value === undefined) return;
    if (latest === null || value > latest) latest = value;
  };
  for (const span of spans) {
    see(span.started_at);
    see(span.ended_at);
    for (const wait of span.waiting_on ?? []) see(wait.since);
    for (const attempt of span.attempts ?? []) {
      see(attempt.started_at);
      see(attempt.ended_at);
    }
    see(span.stop?.requested_at);
  }
  return latest;
}

/** A duration in seconds as the screen writes it: `45s`, `9m`, `2h 05m`. */
export function duration(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  if (whole < 60) return `${whole}s`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`;
}

/** Time into a turn as a row writes it, to the second: `42s`, `1m 04s`, `2h 05m`. */
export function elapsedWords(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(whole / 60);
  if (minutes === 0) return `${whole}s`;
  if (minutes < 60) return `${minutes}m ${String(whole % 60).padStart(2, '0')}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}

/**
 * A metric value as the screen writes it everywhere: grouped, one decimal below a thousand (so a
 * rate of 385.4 reads as the 385.4 the comparison stated, never as 385 or 390), fewer above and
 * more below ten; trailing zeros are dropped.
 */
export function formatValue(value: number): string {
  const abs = Math.abs(value);
  const digits = abs >= 1000 ? 0 : abs >= 100 ? 1 : abs >= 10 ? 2 : abs >= 1 ? 2 : 3;
  return value.toLocaleString('en-US', {maximumFractionDigits: digits});
}

/** A ratio to the baseline as the screen writes it: `1.42×`. */
export function ratio(value: number): string {
  return `${value.toFixed(2)}×`;
}

/**
 * The reason a failed idea's span gives when its own trusted checks rejected it. It is the
 * idea's result, which `failed` already says, so no screen words it as a reason.
 */
export const TURNED_DOWN = 'turned_down';

/** Snake- and kebab-case words as the screen writes them: `failed_check` reads `failed check`. */
export function words(value: string): string {
  return value.replaceAll(/[_-]/g, ' ');
}
