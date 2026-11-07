/**
 * What the catch-up page draws, from a check-in the host stored. The host worked out every figure
 * and froze it: this adds nothing up, windows nothing and compares nothing. Every figure is a field
 * of the facts said in words, a price the host did not report stays absent (`null`) to the screen,
 * and every list is in the order the host sent it. The only arithmetic is the layout's own (how
 * long the window is, for its label, and the bars' scale in `BarRows`).
 */
import type {AgentRole, ObjectiveDirection, Report, WindowFacts} from '@vibesys/orchestrator-proto';
import {ObjectiveDirection as Direction} from '@vibesys/orchestrator-proto';
import type {BarRow} from '@vibesys/ui';
import type {CheckInRead, Summary} from '../../state/index.js';
import {formatDayTime, formatSpan} from '../agents/time.js';
import {formatCount, formatUsd, NOT_REPORTED, precisionOf} from '../cost/words.js';
import {formatFigure} from '../figures.js';

/** The words of the run's own vocabulary this page needs, so it imports nothing from the feature that shows it. */
export interface CatchUpWords {
  /** What a type of agent is called. */
  readonly role: (role: AgentRole) => string;
}

export interface ObjectiveFigures {
  readonly metric: string;
  /** What the values are counted in, as the host served it; empty when it did not. */
  readonly unit: string;
  readonly direction: 'maximize' | 'minimize';
  /** Where the objective stood when the window began; null when nothing was measured before it. */
  readonly start: string | null;
  readonly end: string;
  /** The job and the commit that made the best value at the end of the window. */
  readonly source: string;
}

/** The figures of a window, as the page's tiles write them. */
export interface Tiles {
  readonly spend: {
    /** The cost of the priced turns; null when no turn of the window was priced. */
    readonly total: string | null;
    readonly turns: string;
    /** Turns the total leaves out for want of a price; null when none. */
    readonly unpriced: string | null;
    /** By agent type: turns, cost. */
    readonly byType: readonly BarRow<2>[];
  };
  readonly agents: {readonly started: string; readonly ended: string};
  readonly jobsMeasured: string;
  readonly objectives: readonly ObjectiveFigures[];
  readonly ideas: {readonly created: string; readonly changed: string};
}

/** One part of a report: a label and its text, which is Markdown. */
interface ReportPart {
  readonly label: string;
  readonly text: string;
}

export interface ReportWords {
  readonly headline: string;
  readonly parts: readonly ReportPart[];
}

export type SummaryModel =
  | {readonly kind: 'writing'}
  | {readonly kind: 'report'; readonly report: ReportWords}
  /** The reports posted inside the window, for a run that could not be asked. */
  | {readonly kind: 'reports'; readonly reports: readonly ReportWords[]};

/** Where a window's facts stand, in the word the header chip says. */
type Standing =
  | {readonly kind: 'working'}
  | {readonly kind: 'ready'}
  | {readonly kind: 'failed'; readonly reason: string};

export interface CatchUpModel {
  readonly id: string;
  /** `Since Tue 11:40 PM`, in Seattle time. */
  readonly title: string;
  /** `Until Wed 8:40 AM`. */
  readonly until: string;
  /** How long the window is. */
  readonly span: string;
  readonly standing: Standing;
  /** The figures; null until the host has worked them out, and for a window it could not. */
  readonly tiles: Tiles | null;
  readonly summary: SummaryModel;
}

/** An objective's value as every screen writes it: the host's unit is in the metric's name. */
const formatValue = formatFigure;

const DIRECTIONS = {
  [Direction.UNSPECIFIED]: 'maximize',
  [Direction.MAXIMIZE]: 'maximize',
  [Direction.MINIMIZE]: 'minimize',
} as const satisfies Record<ObjectiveDirection, ObjectiveFigures['direction']>;

/** A usage's cost as the host reported it: absent stays absent, never zero. */
const costOf = (usage: {readonly costUsd?: number | undefined} | undefined): number | null =>
  usage?.costUsd ?? null;

/** The tiles of a window. Every figure is the host's. */
export function tilesOf(facts: WindowFacts, words: CatchUpWords): Tiles {
  const total = costOf(facts.spend?.usage);
  const precision = precisionOf(total);
  const usd = (value: number | null): string | null =>
    value === null ? null : formatUsd(value, precision);
  const unpriced = facts.spend?.unpricedTurns ?? 0;
  return {
    spend: {
      total: usd(total),
      turns: formatCount(facts.spend?.turns ?? 0),
      unpriced: unpriced === 0 ? null : formatCount(unpriced),
      byType: (facts.spend?.roles ?? []).map(role => {
        const cost = costOf(role.usage);
        return {
          key: String(role.role),
          name: words.role(role.role),
          ...(role.unpricedTurns > 0 && cost === null
            ? {note: 'No price'}
            : role.unpricedTurns > 0
              ? {note: `${formatCount(role.unpricedTurns)} unpriced`}
              : {}),
          value: cost,
          figures: [formatCount(role.turns), usd(cost)],
        };
      }),
    },
    agents: {
      started: formatCount(facts.agentsStarted),
      ended: formatCount(facts.agentsEnded),
    },
    jobsMeasured: formatCount(facts.jobsMeasured),
    objectives: facts.objectives.map(objective => ({
      metric: objective.metric,
      unit: objective.unit,
      direction: DIRECTIONS[objective.direction],
      start: objective.start === undefined ? null : formatValue(objective.start.value),
      end: objective.end === undefined ? NOT_REPORTED : formatValue(objective.end.value),
      source: [objective.end?.jobId, objective.end?.commit].filter(part => part).join(' · '),
    })),
    ideas: {
      created: formatCount(facts.plansCreated.length),
      changed: formatCount(facts.plansChanged.length),
    },
  };
}

const PARTS = [
  {label: 'Changed', of: (report: Report) => report.changed},
  {label: 'In flight', of: (report: Report) => report.inFlight},
  {label: 'Decisions', of: (report: Report) => report.decisions},
] as const;

/** A report as its headline and the parts the lead filled in. */
export function reportWords(report: Report): ReportWords {
  return {
    headline: report.headline,
    parts: PARTS.flatMap(part => {
      const text = part.of(report);
      return text.trim() === '' ? [] : [{label: part.label, text}];
    }),
  };
}

function summaryOf(summary: Summary): SummaryModel {
  switch (summary.kind) {
    case 'writing':
      return {kind: 'writing'};
    case 'report':
      return {kind: 'report', report: reportWords(summary.report)};
    case 'reports':
      return {kind: 'reports', reports: summary.reports.map(reportWords)};
  }
}

/** The page's model of one check-in. */
export function catchUpModel(read: CheckInRead, words: CatchUpWords): CatchUpModel {
  const {facts} = read;
  return {
    id: read.checkIn.id,
    title: `Since ${formatDayTime(read.sinceMs)}`,
    until: `Until ${formatDayTime(read.atMs)}`,
    span: formatSpan(read.atMs - read.sinceMs),
    standing:
      facts.kind === 'pending'
        ? {kind: 'working'}
        : facts.kind === 'ready'
          ? {kind: 'ready'}
          : {kind: 'failed', reason: facts.reason},
    tiles: facts.kind === 'ready' ? tilesOf(facts.facts, words) : null,
    summary: summaryOf(read.summary),
  };
}

/**
 * The plans a check-in counted for its window, created then changed: the numbers behind the Created
 * and Changed tiles, the same ones the report's ideas list narrows to. None until the host has
 * worked the window out.
 */
export function windowPlans(read: CheckInRead): readonly number[] {
  if (read.facts.kind !== 'ready') return [];
  return [...read.facts.facts.plansCreated, ...read.facts.facts.plansChanged];
}

/** What a check-in cost, for its row in the list: the host's figure for the window, or why there is none. */
export function costFigure(read: CheckInRead): string | null {
  if (read.facts.kind !== 'ready') return null;
  const total = costOf(read.facts.facts.spend?.usage);
  return total === null ? NOT_REPORTED : formatUsd(total, precisionOf(total));
}
