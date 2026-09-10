/**
 * The header line: what the run is doing, in operator terms.
 *
 * It replaces a line that interpolated backend identifiers verbatim
 * (`VibeSys · running · implementer · round-1-retry-1-implementer · 118k/400k
 * tokens · Preallocated lock-free SPSC ring · r1`). Three things were wrong
 * with that and are fixed here:
 *
 * - Raw `round_label` strings meant nothing to an operator and duplicated the
 *   rounds strip and Agents pane. `describePhase` turns them into words.
 * - The token meter divided one number by another that does not bound it, so
 *   it could read 118 percent and look like an overflow error. `usageText`
 *   only prints a denominator when the numerator is actually inside it.
 * - Everything was concatenated with no width budget, so at 100 columns the
 *   hypothesis title, the one segment carrying what the run is trying, was
 *   clipped while `round-1-retry-1-implementer` survived. `renderHeader` drops
 *   whole segments in a defined order instead, and budgets in terminal cells
 *   rather than UTF-16 code units, which are not the same count.
 * - Every segment was drawn in one colour, so a line that already knew which
 *   part mattered read as an undifferentiated string. `renderHeader` returns
 *   the budgeted segments rather than a joined line, and `headerSpanStyle`
 *   gives each role a tone: the run state carries a verdict, the phase and the
 *   title are content, the metadata recedes.
 */
import {hasRunEnded} from '@vibesys/core-state';
import {runStatusLabel, type SessionState} from '../session-model.js';
import {agentKindText, describePhase, phaseText} from './phase-label.js';
import {displayWidth, truncateToWidth} from './text-width.js';
import type {Theme} from './theme.js';

/** Separator between header segments, matching the rest of the interface. */
const SEPARATOR = ' · ';

const BRAND = 'VibeSys';

/**
 * One header segment and how readily it is given up when the line does not
 * fit. Lower priority goes first.
 *
 * The order encodes the judgement in #517: the hypothesis title outranks the
 * token meter, the selected-agent note, and the dialog hint, because it is the
 * only segment that says what the run is trying to achieve.
 */
interface Segment {
  text: string;
  /** What the segment is, which is what decides its tone. */
  role: HeaderRole;
  priority: number;
  /**
   * Never dropped. The brand and the run state are the header's floor, which
   * is what `MIN_WIDTH` is the width of.
   */
  required?: boolean;
  /**
   * Shortened to fit rather than dropped. Only the hypothesis title is: half a
   * claim still says which claim, where a dropped one says nothing. Every other
   * segment is a fixed phrase that means nothing in part.
   */
  truncatable?: boolean;
}

/**
 * The title outranks the phase deliberately. The phase is also on the Agents
 * pane and implied by the transcript; the title is the only place the run says
 * what it is trying, and #517 requires it to survive a narrow terminal ahead of
 * less useful segments.
 */
const PRIORITY = {
  brand: 100,
  state: 90,
  title: 70,
  phase: 60,
  usage: 40,
  selection: 30,
  hint: 10,
} as const;

/** What a segment is. `headerSegments` emits at most one segment per role. */
export type HeaderRole = keyof typeof PRIORITY;

/** A drawn run of the header: one segment, or one separator between two. */
export type HeaderSpanRole = HeaderRole | 'separator';

/**
 * One run of the header line, in the order it is drawn.
 *
 * A terminal cell carries one foreground colour, so a header with internal
 * hierarchy is several renderables laid out in a row rather than one string.
 * The separators are spans of their own so the dots can recede behind the words
 * they divide.
 */
export interface HeaderSpan {
  text: string;
  role: HeaderSpanRole;
}

/** How one span is drawn. */
export interface HeaderSpanStyle {
  fg: string;
  bold: boolean;
}

/**
 * The most spans `renderHeader` can return: every role at once, with a
 * separator between each pair. A caller that keeps one renderable per span can
 * size its row from this rather than rebuilding the row on every frame.
 */
export const MAX_HEADER_SPANS = Object.keys(PRIORITY).length * 2 - 1;

/** Cells below which a shortened title is noise rather than an identifier. */
const MIN_TITLE = 12;

/**
 * The one run state that is not a backend status: the event stream is gone, so
 * whatever the status last said can no longer be trusted to be current.
 */
const DISCONNECTED = 'disconnected';

/** The widest word `runStateText` produces; every status label is shorter. */
const WIDEST_STATE = DISCONNECTED;

/**
 * The narrowest terminal this header supports, in cells.
 *
 * At this width or wider the brand and the run state both render whole: they
 * are the two segments `renderHeader` will not drop, and no run state is wider
 * than `WIDEST_STATE`. Below it neither is guaranteed, because there is no
 * room for both: the line is cut and marked with an ellipsis, which is what a
 * terminal that narrow can be told. That bound is the property the header
 * holds; "the brand and the run state always survive" is not true at every
 * width and was never true of the dropping loop.
 */
export const MIN_WIDTH = displayWidth(`${BRAND}${SEPARATOR}${WIDEST_STATE}`);

/**
 * Run state in words.
 *
 * The backend owns the run's lifecycle and publishes every move through it, so
 * the run state is the run status and `runStatusLabel` is how it reads. There
 * is no client-local pause flag to prefix it or to outrank it, which is what
 * keeps the header from claiming a state the backend disagrees with.
 *
 * The header adds one word of its own. Without a trustworthy event stream the
 * status is only as current as the last event that arrived, so a live run is
 * reported as `disconnected` rather than as a value that has stopped moving. A
 * run that has ended is exempt: a status it never leaves cannot go stale, and a
 * finished run whose socket closed is finished rather than disconnected.
 * `hasRunEnded` is the authority on which statuses those are, and it switches
 * exhaustively, so a status added to the protocol is a compile error there
 * rather than a run this header quietly reports as live.
 */
export function runStateText(state: SessionState): string {
  if (!hasRunEnded(state.core) && !state.eventStreamAvailable) return DISCONNECTED;
  return runStatusLabel(state.core.status);
}

/**
 * The token meter, with an honest denominator or none at all.
 *
 * `inputTokens` is the context the last agent call carried, and `contextWindow`
 * is a static per-model lookup that can be absent or stale. When the two
 * disagree, the count alone is the true statement; a ratio over 100 percent is
 * not.
 */
export function usageText(state: SessionState): string | null {
  const usage = state.core.usage;
  if (usage === null || usage.inputTokens <= 0) return null;
  const used = formatTokenCount(usage.inputTokens);
  if (usage.contextWindow === null || usage.inputTokens > usage.contextWindow) {
    return `${used} tokens`;
  }
  // Labeled `context` rather than `tokens`: the denominator bounds one call's
  // context, and calling it "tokens" invited reading it as run spend.
  return `${used}/${formatTokenCount(usage.contextWindow)} context`;
}

function formatTokenCount(count: number): string {
  if (count < 1_000) return String(count);
  if (count < 1_000_000) return `${Math.floor(count / 1_000)}k`;
  return `${(count / 1_000_000).toFixed(1)}M`;
}

/** Segments for the current state, before any width budgeting. */
export function headerSegments(state: SessionState, showLog: boolean): Segment[] {
  const segments: Segment[] = [
    {text: BRAND, role: 'brand', priority: PRIORITY.brand, required: true},
    {text: runStateText(state), role: 'state', priority: PRIORITY.state, required: true},
  ];

  if (showLog) {
    segments.push({text: 'experiments', role: 'phase', priority: PRIORITY.phase});
  } else {
    const phase = phaseText(describePhase(state.core.roundLabel, state.core.agentKind));
    if (phase !== null) segments.push({text: phase, role: 'phase', priority: PRIORITY.phase});
    if (state.hypothesisScope !== null) {
      segments.push({
        text: state.hypothesisScope.title,
        role: 'title',
        priority: PRIORITY.title,
        truncatable: true,
      });
    }
  }

  const usage = usageText(state);
  if (usage !== null) segments.push({text: usage, role: 'usage', priority: PRIORITY.usage});

  // Through the same table the phase segment reads, because the selection is a
  // phase kind and the header prints no phase kind raw. `perf_eval` is the one
  // the plain loop seeds and the operator can select, and `selected perf_eval`
  // is exactly the backend identifier #517 removed. A kind with no word is
  // dropped rather than spelled out: this note is the second cheapest segment
  // on the line, so saying nothing costs less than saying an identifier.
  const selected = showLog ? null : agentKindText(state.selectedAgentKind);
  if (selected !== null) {
    segments.push({
      text: `filtered to ${selected}`,
      role: 'selection',
      priority: PRIORITY.selection,
    });
  }

  const dialogOpen = state.chatOpen || state.overlay !== null || state.themePicker !== null;
  if (dialogOpen) {
    segments.push({text: 'Esc: close dialog', role: 'hint', priority: PRIORITY.hint});
  }

  return segments;
}

/**
 * Assembles the header to fit `width` terminal cells.
 *
 * Whole segments are dropped, lowest priority first, rather than clipping the
 * line: a truncated fixed phrase reads as a rendering bug, while a missing
 * token meter reads as a header that had better things to say. Only once
 * everything droppable is gone does the title shorten, and only then, which is
 * what keeps it from being clipped ahead of less useful segments.
 *
 * The budget is in cells, not code units, so a header the caller sized for 40
 * columns of CJK does not lay out over 50 of them, and the fallback cut lands
 * on a grapheme boundary rather than inside an emoji.
 *
 * At `MIN_WIDTH` and wider the brand and the run state both survive whole.
 * Narrower than that they do not fit together, and the line is cut and marked.
 *
 * The result is the budgeted spans in draw order, not a joined line, because a
 * terminal cell carries one foreground colour and the roles do not share one.
 * Concatenating the span texts reproduces the line exactly.
 */
export function renderHeader(state: SessionState, showLog: boolean, width: number): HeaderSpan[] {
  const kept = headerSegments(state, showLog);
  while (displayWidth(joined(kept)) > width) {
    const weakest = weakestIndex(kept);
    // Only the brand and the run state are left, and they do not fit: below
    // MIN_WIDTH the line is cut rather than emptied.
    if (weakest === null) break;
    // Nothing left that is cheaper than the title: shortening it now is the
    // only move that does not cost a whole segment, and by this point it is no
    // longer being clipped ahead of anything less useful.
    if (kept[weakest]?.truncatable === true && shrinkTitle(kept, width)) break;
    kept.splice(weakest, 1);
  }
  const spans = toSpans(kept);
  if (displayWidth(joined(kept)) <= width) return spans;
  if (width <= 0) return [];
  return cutSpans(spans, width);
}

/**
 * The surface the header paints, and therefore the surface its text is read
 * against.
 *
 * One definition for both: `app.ts` fills the frame with it and the tones below
 * are held against it. Two independent statements of where the header sits is
 * how a contrast guarantee comes to be measured against a background nothing
 * draws.
 *
 * `canvas`, because that is the only background the theme has (#574). This
 * returned `elevatedSurface` until then. The cells the header paints have not
 * changed colour: the collapse kept the value the panes and the header already
 * used and brought `canvas` down to meet it. What changed is that the header
 * now matches everything around it and not only the other panes.
 */
export function headerBackground(theme: Theme): string {
  return theme.canvas;
}

/**
 * Colour and weight for one span, against `headerBackground`.
 *
 * Four levels. The run state is the one fact the header exists to report, so it
 * is bold and carries a verdict colour. The brand is a masthead in the accent:
 * fixed text that anchors the left edge and never reports anything. The phase
 * and the title are what the run is doing and trying, so they stay in body
 * text. The token meter, the selected-agent note and the dialog hint are
 * metadata and recede to muted. The separators recede further still: dots
 * divide the words without competing with them.
 *
 * Every colour comes from the theme as `buildTheme` made it, which is enough
 * because the header paints the canvas and `buildTheme` holds each of these
 * tokens to `minContrast` against exactly that. Each tone used to be put back
 * through `ensureContrast` here; that pass existed only to cross from the
 * canvas to a second surface, and since #574 there is no second surface to
 * cross to.
 *
 * `textSubtle` is the exception the theme itself makes, at a floor of 3 rather
 * than 4.5 or 7, which is why only the punctuation is drawn in it and every
 * word clears the full floor.
 */
export function headerSpanStyle(theme: Theme, span: HeaderSpan): HeaderSpanStyle {
  switch (span.role) {
    case 'brand':
      return {fg: theme.accent, bold: true};
    case 'state':
      return {fg: stateColor(theme, span.text), bold: true};
    case 'phase':
    case 'title':
      return {fg: theme.textPrimary, bold: false};
    case 'usage':
    case 'selection':
    case 'hint':
      return {fg: theme.textMuted, bold: false};
    case 'separator':
      return {fg: theme.textSubtle, bold: false};
  }
}

/**
 * The verdict the run state carries.
 *
 * Compared against `runStatusLabel`'s own output rather than against the raw
 * status words, because the label is what `runStateText` put on the line and
 * the two are not always the same string: `pausing` renders as `pausing…`, and
 * a literal here would silently stop matching it.
 *
 * `running`, `starting` and `connecting` are not verdicts, and neither is a
 * status the backend adds after this was written, so they stay in body text
 * rather than being forced into one. What sets the state apart in those cases
 * is the bold `headerSpanStyle` gives it, which is the reason the state is bold
 * at all: colour alone would say nothing on the statuses that have no colour,
 * and nothing at all on a terminal that drops it. The word is always spelled
 * out.
 *
 * `pausing` shares the warning of `paused` rather than getting a tone of its
 * own: it is the same verdict, and the word is what says the pause has not
 * landed yet. A colour the operator has to learn would say it less clearly.
 */
function stateColor(theme: Theme, state: string): string {
  if (state === runStatusLabel('completed')) return theme.success;
  if (state === runStatusLabel('failed')) return theme.error;
  if (state === runStatusLabel('pausing') || state === runStatusLabel('paused')) {
    return theme.warning;
  }
  if (state === DISCONNECTED) return theme.warning;
  return theme.textPrimary;
}

/** Segments in draw order with the separators between them made explicit. */
function toSpans(segments: Segment[]): HeaderSpan[] {
  const spans: HeaderSpan[] = [];
  for (const segment of segments) {
    if (spans.length > 0) spans.push({text: SEPARATOR, role: 'separator'});
    spans.push({text: segment.text, role: segment.role});
  }
  return spans;
}

/**
 * Cuts the spans to `width` and marks the cut, for the widths below `MIN_WIDTH`
 * where not even the brand and the run state fit together. Equivalent to
 * cutting the joined line, but it keeps the surviving text in its own spans so
 * the tones hold to the last cell.
 */
function cutSpans(spans: HeaderSpan[], width: number): HeaderSpan[] {
  // One cell for the ellipsis that says the rest was cut.
  const budget = width - 1;
  const cut: HeaderSpan[] = [];
  let used = 0;
  for (const span of spans) {
    if (used >= budget) break;
    const text = truncateToWidth(span.text, budget - used);
    const fitted = displayWidth(text);
    if (fitted > 0) {
      cut.push({...span, text});
      used += fitted;
    }
    // A span that did not survive whole ends the line, whether it was cut mid
    // text or dropped entirely by a wide grapheme straddling the budget.
    if (fitted < displayWidth(span.text)) break;
  }
  const last = cut.at(-1);
  if (last === undefined) return [{text: '…', role: spans[0]?.role ?? 'brand'}];
  cut[cut.length - 1] = {...last, text: `${last.text}…`};
  return cut;
}

/** The segment given up first, or null once only required ones are left. */
function weakestIndex(segments: Segment[]): number | null {
  let weakest: number | null = null;
  for (const [index, candidate] of segments.entries()) {
    if (candidate.required === true) continue;
    const current = weakest === null ? undefined : segments[weakest];
    // `<=` makes the later of two equal-priority segments the weaker one, so
    // trailing notes go before leading ones.
    if (current === undefined || candidate.priority <= current.priority) weakest = index;
  }
  return weakest;
}

/**
 * Shortens the truncatable segment to close the overrun, if what remains is
 * still long enough to identify. Returns whether the line now fits.
 */
function shrinkTitle(segments: Segment[], width: number): boolean {
  const index = segments.findIndex(segment => segment.truncatable === true);
  const segment = segments[index];
  if (segment === undefined) return false;
  // What the rest of the line, its separators included, already spends.
  const rest = displayWidth(joined(segments)) - displayWidth(segment.text);
  // One cell of the budget goes to the ellipsis.
  const budget = width - rest - 1;
  if (budget < MIN_TITLE) return false;
  const shortened = truncateToWidth(segment.text, budget).trimEnd();
  // A wide grapheme dropped at the cut, or trailing space, can leave less than
  // the budget allowed, and below MIN_TITLE the caller is better off dropping.
  if (displayWidth(shortened) < MIN_TITLE) return false;
  segments[index] = {...segment, text: `${shortened}…`};
  return displayWidth(joined(segments)) <= width;
}

function joined(segments: Segment[]): string {
  return segments.map(segment => segment.text).join(SEPARATOR);
}
