/**
 * What an agent turn ends with, as the card the step view draws last. A turn's structured reply
 * is read by its kind (the reply model's name): the orchestrator's plan has a reader that lays it
 * out for reviewing (the idea's title, what it builds on, its claim, its forecast, its reasoning,
 * the gates it must pass, and what it changes in the others); any other reply keeps the generic
 * reading of its fields (its choices as chips, its summary), with every other field after them. The card's text is the agent's own and is never cut; only the one-line `summary` that
 * leads the turn is.
 */
import type {Candidate} from '@vibesys/run-state/candidates';
import type {ReplyFieldView} from './reply-fields.js';
import {words} from './span-reading.js';
import {replyOverview} from './step-narrative.js';
import type {TurnRow} from './turn-transcript.js';
import type {Tone} from './views.js';

type ReplyRow = Extract<TurnRow, {kind: 'reply'}>;
type Json = Readonly<Record<string, unknown>>;

/** The orchestrator's reply model (`OrchestratorPlan` in `src/vibesys/hypothesis/plan.py`). */
const PLAN_KIND = 'OrchestratorPlan';
const SUMMARY_LIMIT = 120;

/** One block of the card's body, in reading order. */
type ResultSection =
  | {readonly kind: 'prose'; readonly key: string; readonly label: string; readonly text: string}
  | {
      readonly kind: 'list';
      readonly key: string;
      readonly label: string;
      readonly items: readonly string[];
    };

/** What the idea builds on: the input, or an idea (`idea` opens it; null when the run lacks it). */
interface BuiltOn {
  readonly label: string;
  readonly idea: string | null;
}

/** A turn's reply as the card draws it. */
export interface TurnResultView {
  /** The reply's row id. */
  readonly id: string;
  /** What the card is: `Plan`, or the reply's kind as a title. */
  readonly label: string;
  readonly author: string;
  /** The big line (a plan's idea title); null when the reply has none. */
  readonly headline: string | null;
  readonly chips: readonly {readonly label: string; readonly tone?: Tone}[];
  readonly builtOn: BuiltOn | null;
  readonly sections: readonly ResultSection[];
  /** The fields that follow the body, in the reply's order. */
  readonly more: readonly ReplyFieldView[];
  readonly raw: string;
  /** The one line that leads the turn: `Plan: Pad the counters · builds on input`. */
  readonly summary: string;
}

/** The run as far as a reply needs it: the ideas, and the idea the turn belongs to. */
export interface ResultContext {
  readonly candidates: readonly Candidate[];
  /** The idea the opened step belongs to; null under an opened agent. */
  readonly idea: Candidate | null;
}

function text(value: Json, key: string): string {
  const found = value[key];
  return typeof found === 'string' ? found.trim() : '';
}

function records(value: Json, key: string): Json[] {
  const found = value[key];
  if (!Array.isArray(found)) return [];
  return found.filter(
    (item): item is Json => typeof item === 'object' && item !== null && !Array.isArray(item),
  );
}

const INPUT: BuiltOn = {label: 'input', idea: null};

function ideaBuiltOn(found: Candidate): BuiltOn {
  return {label: found.title, idea: found.id};
}

/**
 * What the plan's idea builds on. The plan's `base_round` names it (0 is the input); a plan that
 * names none builds on the best so far, which the idea records once it started, so it reads from
 * the idea the turn belongs to; with neither, the card says nothing.
 */
function builtOnOf(plan: Json, context: ResultContext): BuiltOn | null {
  const round = plan['base_round'];
  if (typeof round === 'number') {
    if (round === 0) return INPUT;
    const found = context.candidates.find(each => each.round === round);
    return found === undefined ? {label: `idea ${round}`, idea: null} : ideaBuiltOn(found);
  }
  const idea = context.idea;
  if (idea === null) return null;
  if (idea.parent === null) return INPUT;
  const parent = context.candidates.find(each => each.id === idea.parent);
  return parent === undefined ? null : ideaBuiltOn(parent);
}

function oneLine(value: string): string {
  const line = value.split('\n', 1)[0]?.trim() ?? '';
  return line.length > SUMMARY_LIMIT ? `${line.slice(0, SUMMARY_LIMIT - 1)}…` : line;
}

function updateLines(plan: Json): string[] {
  return records(plan, 'hypothesis_updates').map(
    each => `${text(each, 'hypothesis_id')}: ${text(each, 'disposition')}. ${text(each, 'reason')}`,
  );
}

function roadmapLines(plan: Json): string[] {
  return records(plan, 'roadmap').map(each => {
    const note = text(each, 'note');
    return `${text(each, 'title')} (${text(each, 'status')})${note === '' ? '' : `. ${note}`}`;
  });
}

/** The plan's body in reading order: what it claims and expects, why, and how it is judged. */
function planSections(plan: Json): ResultSection[] {
  const prose = (key: string, label: string): ResultSection[] =>
    text(plan, key) === '' ? [] : [{kind: 'prose', key, label, text: text(plan, key)}];
  const list = (key: string, label: string, items: readonly string[]): ResultSection[] =>
    items.length === 0 ? [] : [{kind: 'list', key, label, items}];
  return [
    ...prose('hypothesis', 'Hypothesis'),
    ...prose('expected_effect', 'Expected effect'),
    ...prose('reasoning', 'Reasoning'),
    ...prose('pass_criteria', 'Pass criteria'),
    ...prose('falsification_criteria', 'Falsification criteria'),
    ...list('hypothesis_updates', 'Updates to other hypotheses', updateLines(plan)),
    ...list('roadmap', 'Roadmap changes', roadmapLines(plan)),
  ];
}

function summaryLine(label: string, parts: readonly string[]): string {
  const rest = parts.filter(part => part !== '').join(' · ');
  return rest === '' ? label : `${label}: ${rest}`;
}

function planResult(reply: ReplyRow, context: ResultContext): TurnResultView {
  const sections = planSections(reply.value);
  const shown = new Set([...sections.map(section => section.key), 'title']);
  const headline = text(reply.value, 'title');
  // Only the orchestrator plans a base; a turn of another role (a judge) builds on nothing.
  const builtOn = reply.role === 'orchestrator' ? builtOnOf(reply.value, context) : null;
  return {
    id: reply.id,
    label: 'Plan',
    author: reply.author,
    headline: headline === '' ? null : headline,
    chips: [],
    builtOn,
    sections,
    more: reply.fields.filter(field => !shown.has(field.key)),
    raw: reply.raw,
    summary: summaryLine('Plan', [headline, builtOn === null ? '' : `builds on ${builtOn.label}`]),
  };
}

/** The card is the response, so its label does not say so: `Judge response` reads as `Judge`. */
function cardLabel(title: string): string {
  const bare = title.replace(/\s+(response|reply)$/i, '');
  return bare === '' ? title : bare;
}

/** Any other reply: its choices as chips, its summary, and every other field after them. */
function genericResult(reply: ReplyRow): TurnResultView {
  const overview = new Set(replyOverview(reply.fields));
  const chips = reply.fields.flatMap(field =>
    field.kind === 'badge'
      ? [{label: words(field.value), ...(field.tone === undefined ? {} : {tone: field.tone})}]
      : [],
  );
  const sections = reply.fields.flatMap((field): ResultSection[] =>
    field.kind === 'prose' && overview.has(field.key)
      ? [{kind: 'prose', key: field.key, label: field.label, text: field.text}]
      : [],
  );
  const lead = sections[0];
  return {
    id: reply.id,
    label: cardLabel(reply.title),
    author: reply.author,
    headline: null,
    chips,
    builtOn: null,
    sections,
    more: reply.fields.filter(field => field.kind !== 'badge' && !overview.has(field.key)),
    raw: reply.raw,
    summary: summaryLine(reply.title, [
      ...chips.map(chip => chip.label),
      lead?.kind === 'prose' ? oneLine(lead.text) : '',
    ]),
  };
}

/** The card for `reply`, by its kind. */
export function turnResult(reply: ReplyRow, context: ResultContext): TurnResultView {
  return reply.replyKind === PLAN_KIND ? planResult(reply, context) : genericResult(reply);
}
