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
export function kindOf(span: Span): string | null {
  return span.label?.kind ?? null;
}

/** What a span is called: its label's title, else its registered name. */
export function titleOf(span: Span): string {
  const title = span.label?.title;
  return title === undefined || title === null || title === '' ? span.name : title;
}

/** The label's attributes as a record; empty when it has none or they do not parse to one. */
function attrsOf(span: Span): Readonly<Record<string, unknown>> {
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

/** Every descendant of `span`, depth first in start order. */
export function descendants(index: SpanIndex, span: Span): Span[] {
  const found: Span[] = [];
  const seen = new Set<string>([span.id]);
  const visit = (id: string): void => {
    for (const child of index.children.get(id) ?? []) {
      if (seen.has(child.id)) continue;
      seen.add(child.id);
      found.push(child);
      visit(child.id);
    }
  };
  visit(span.id);
  return found;
}

/** Whether the span is still under way: running, waiting, or not yet started. */
export function isOpen(span: Span): boolean {
  return span.status === 'running' || span.status === 'waiting' || span.status === 'pending';
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

/** The earliest run-clock time a span started at, or null with no spans. */
export function earliestTime(spans: readonly Span[]): number | null {
  let earliest: number | null = null;
  for (const span of spans) {
    if (earliest === null || span.started_at < earliest) earliest = span.started_at;
  }
  return earliest;
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

/** A ratio to the baseline as the screen writes it: `1.42×`. */
export function ratio(value: number): string {
  return `${value.toFixed(2)}×`;
}

/** Snake-case words as the screen writes them: `failed_check` reads `failed check`. */
export function words(value: string): string {
  return value.replaceAll('_', ' ');
}
