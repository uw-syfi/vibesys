/**
 * The generic run overview as plain rows and words. `presentRunDetail` groups the host's typed facts;
 * this decides how each is written, once, for the screen and its tests: a fact the run did not
 * record reads as a dash or as `unknown`, never as zero; a name a strategy chose is read as words;
 * and nothing here prices, ranks or sums a figure the host did not send.
 */
import {roleName} from '@vibesys/display-names';
import type {Figure} from '@vibesys/ui';
import type {GenericRunEntry, RunDetailPresentation} from '../../../state/index.js';
import {
  type CostCell,
  costCell,
  formatCount,
  formatUsd,
  NOT_REPORTED,
  precisionOf,
  type TokenSplit,
  tokenSplit,
} from '../../cost/index.js';
import {formatFigure} from '../../figures.js';
import {plural} from '../../plain-words.js';
import {yet} from '../report/yet.js';
import type {Word} from '../words.js';
import {
  availabilityNote,
  inputFailureWord,
  isEnded,
  lifecycleWord,
  metricRoleWord,
  openWords,
  stageWord,
  subjectWord,
  workstreamFailureWord,
} from './words.js';

type Presentation = RunDetailPresentation;
type Agent = Presentation['agents']['items'][number];
type Metric = Presentation['optimization']['metrics']['items'][number];
type Workstream = Presentation['workstreams']['items'][number];
type Gate = Presentation['correctness']['items'][number];

const DASH = NOT_REPORTED;

/** One section of the overview: its rows, and the words that say why it is short or missing. */
export interface SectionView<Row> {
  readonly rows: readonly Row[];
  /** Why the section holds less than the run did, or null when the host served it whole. */
  readonly note: string | null;
  /** What the section says with no row: why, when the host said why, else that nothing came yet. */
  readonly empty: string;
}

export interface MeasureRow {
  readonly key: string;
  readonly name: string;
  readonly role: string;
  /** The newest reading with its unit, or a dash when none was recorded. */
  readonly latest: string;
  /** The change from the oldest reading kept to the newest, signed, or a dash. */
  readonly change: string;
  readonly readings: string;
  /** Which way is better, and what the change is measured from. */
  readonly hint: string;
}

export interface AgentRow {
  readonly key: string;
  readonly name: string;
  readonly stage: string;
  readonly state: Word;
  readonly model: string;
  readonly modelHint: string;
  readonly tokens: TokenSplit;
  readonly cost: CostCell;
}

export interface WorkstreamRow {
  readonly key: string;
  readonly title: string;
  readonly state: Word;
  readonly round: string;
  readonly result: string;
  readonly failed: boolean;
  /** The whole account: the idea or summary the host recorded, and any failure text. */
  readonly hint: string;
}

export interface CheckRow {
  readonly key: string;
  readonly title: string;
  readonly check: string;
  readonly subject: string;
  readonly state: Word;
  readonly result: string;
  readonly failed: boolean;
  readonly evidence: string;
  /** The kind of check the evidence is of, with the evidence in full. */
  readonly evidenceHint: string;
  readonly source: string;
}

export interface GenericRunOverview {
  readonly figures: readonly Figure[];
  readonly measures: SectionView<MeasureRow>;
  readonly agents: SectionView<AgentRow>;
  readonly workstreams: SectionView<WorkstreamRow>;
  readonly checks: SectionView<CheckRow>;
  /** How many records the app could not read, in words; null when every record was. */
  readonly unreadable: string | null;
  /** Older entries the app dropped to stay in bounds, in words; null when it dropped none. */
  readonly dropped: string | null;
}

const section = <Source, Row>(
  source: {
    readonly items: readonly Source[];
    readonly availability: Parameters<typeof availabilityNote>[0];
  },
  rows: readonly Row[],
  none: string,
): SectionView<Row> => {
  const note = availabilityNote(source.availability);
  return {
    rows,
    note,
    empty: source.availability.kind === 'unavailable' && note !== null ? note : none,
  };
};

/** A change as a signed percent, or as a multiple once it is too large for a percent to read. */
const signed = (percent: number): string => {
  const size = Math.abs(percent);
  if (percent >= 1000) return `${formatFigure(percent / 100 + 1)}×`;
  const text = size < 10 ? size.toFixed(1) : String(Math.round(size));
  return `${percent < 0 ? '−' : '+'}${text}%`;
};

const DIRECTION: Readonly<Record<Metric['direction'], string>> = {
  max: 'Higher is better.',
  min: 'Lower is better.',
  none: '',
};

function measureRow(metric: Metric): MeasureRow {
  const [first] = metric.points;
  const {latest} = metric;
  const readings =
    metric.omittedPoints > 0
      ? `${formatCount(metric.points.length)} of ${formatCount(metric.points.length + metric.omittedPoints)}`
      : formatCount(metric.points.length);
  const base = first === undefined || first === latest ? null : first.value;
  const change =
    latest === null || base === null || base === 0
      ? DASH
      : signed(((latest.value - base) / Math.abs(base)) * 100);
  const kept =
    metric.omittedPoints > 0
      ? `${formatCount(metric.omittedPoints)} older ${plural(metric.omittedPoints, 'reading was', 'readings were')} left out. `
      : '';
  return {
    key: `${metric.name}:${metric.role}`,
    name: openWords(metric.name),
    role: metricRoleWord(metric.role),
    latest: latest === null ? DASH : `${formatFigure(latest.value)} ${metric.unit}`.trim(),
    change,
    readings: metric.points.length === 0 ? DASH : readings,
    hint: [
      DIRECTION[metric.direction],
      base === null ? '' : `Change since the oldest reading kept, ${formatFigure(base)}.`,
      kept,
    ]
      .filter(part => part !== '')
      .join(' '),
  };
}

const ROLE_ORDER: Readonly<Record<Metric['role'], number>> = {
  objective: 0,
  resource: 1,
  reference: 2,
};

/** The objectives first, then what the run spent, then references, each in the order recorded. */
const measuresOf = (metrics: readonly Metric[]): readonly MeasureRow[] =>
  [...metrics].sort((a, b) => ROLE_ORDER[a.role] - ROLE_ORDER[b.role]).map(measureRow);

function agentRow(agent: Agent, precision: 0 | 2): AgentRow {
  const {usage} = agent;
  // An ended agent that reported nothing is unknown; one still working has not reported yet.
  const reported = usage !== undefined || isEnded(agent.lifecycle);
  const turns = reported ? 1 : 0;
  const split = tokenSplit(
    {
      inputTokens: toCount(usage?.inputTokens),
      cacheReadTokens: toCount(usage?.cacheReadTokens),
      cacheCreationTokens: toCount(usage?.cacheCreationTokens),
      outputTokens: toCount(usage?.outputTokens),
      reasoningTokens: toCount(usage?.reasoningTokens),
      costUsd: usage?.costUsd ?? null,
    },
    turns,
  );
  const model =
    agent.provider === undefined || agent.provider === ''
      ? (agent.model ?? '')
      : `${agent.provider} · ${agent.model ?? ''}`.replace(/ · $/, '');
  return {
    key: agent.spanId,
    name: roleName(agent.role),
    stage: agent.stage === '' ? DASH : openWords(agent.stage),
    state: lifecycleWord(agent.lifecycle),
    model: model === '' ? DASH : model,
    modelHint: [
      model,
      agent.reasoningEffort === undefined ? '' : `Reasoning effort ${agent.reasoningEffort}`,
    ]
      .filter(part => part !== '')
      .join('\n'),
    tokens: split,
    cost: costCell({costUsd: usage?.costUsd ?? null}, turns, 0, precision),
  };
}

/** A count the host sent as a 64-bit integer; a count past the safe range reads as unknown. */
const toCount = (value: bigint | undefined): number | null =>
  value === undefined || value > BigInt(Number.MAX_SAFE_INTEGER) ? null : Number(value);

function workstreamResult(stream: Workstream): {readonly text: string; readonly failed: boolean} {
  const {workstreamFailure: failure, spanFailure} = stream;
  if (failure !== undefined) {
    const turns =
      failure.turns !== undefined && failure.limit !== undefined
        ? ` (${failure.turns} of ${failure.limit} turns)`
        : '';
    return {text: `${workstreamFailureWord(failure.kind)}${turns}`, failed: true};
  }
  if (spanFailure !== undefined)
    return {
      text: spanFailure.kind === 'interruption' ? 'Interrupted' : 'Failed',
      failed: spanFailure.kind !== 'interruption',
    };
  const said = stream.outcome ?? stream.verdict;
  return {text: said === undefined || said === '' ? DASH : openWords(said), failed: false};
}

function workstreamRow(stream: Workstream): WorkstreamRow {
  const result = workstreamResult(stream);
  return {
    key: stream.spanId,
    title: stream.title,
    state: lifecycleWord(stream.lifecycle),
    round: stream.round === undefined ? DASH : String(stream.round),
    result: result.text,
    failed: result.failed || stream.lifecycle === 'failed',
    hint: [
      stream.title,
      stream.idea === undefined || stream.idea === stream.title ? '' : stream.idea,
      stream.summary ?? '',
      stream.workstreamFailure?.text ?? '',
    ]
      .filter(part => part !== '')
      .join('\n'),
  };
}

/** What the failed check had reached: `93 of 100 trials completed`, and which kind of check it was. */
function checkEvidence(gate: Gate): {readonly text: string; readonly hint: string} {
  const {failedCheck} = gate;
  if (failedCheck === undefined) return {text: DASH, hint: ''};
  const kind = failedCheck.check === 'correctness' ? 'Correctness check' : 'Benchmark';
  if (failedCheck.completed === undefined || failedCheck.required === undefined)
    return {text: DASH, hint: kind};
  const unit = failedCheck.unit === undefined ? '' : ` ${failedCheck.unit}`;
  const text = `${failedCheck.completed} of ${failedCheck.required}${unit} completed`;
  return {text, hint: `${kind}: ${text}`};
}

function checkResult(gate: Gate): {readonly text: string; readonly failed: boolean} {
  if (gate.inputFailure !== undefined)
    return {text: inputFailureWord(gate.inputFailure.kind), failed: true};
  const failed = gate.failedCheck !== undefined || gate.lifecycle === 'failed';
  if (gate.verdict !== undefined) return {text: openWords(gate.verdict), failed};
  if (failed) return {text: 'Check failed', failed};
  return {
    text: gate.lifecycle === 'done' ? 'Finished' : lifecycleWord(gate.lifecycle).label,
    failed,
  };
}

function checkRow(gate: Gate): CheckRow {
  const result = checkResult(gate);
  const evidence = checkEvidence(gate);
  return {
    key: gate.spanId,
    title: gate.title,
    check: stageWord(gate.stage),
    subject: subjectWord(gate.subject),
    state: lifecycleWord(gate.lifecycle),
    result: gate.reused ? `${result.text} (reused)` : result.text,
    failed: result.failed,
    evidence: evidence.text,
    evidenceHint: evidence.hint,
    source: gate.sourceLabel === undefined ? DASH : openWords(gate.sourceLabel),
  };
}

/** A figure nobody reported: a dash, never a zero, with what is missing under it. */
const unreported = (label: string, detail: string): Figure => ({
  label,
  value: DASH,
  detail,
  tone: 'muted',
});

function bestFigure(run: GenericRunEntry): Figure {
  const {objective} = run;
  if (objective === null) return unreported('Best so far', 'No objective recorded');
  if (objective.bestValue === null) return unreported('Best so far', 'Nothing measured yet');
  // The unit is in the line under the value, so a slot never cuts the figure itself short.
  return {
    label: 'Best so far',
    value: formatFigure(objective.bestValue),
    detail: [openWords(objective.name), objective.unit].filter(part => part !== '').join(' · '),
  };
}

function spendFigure(run: GenericRunEntry): Figure {
  if (run.costUsd === null) return unreported('Spend', 'Not reported');
  return {
    label: 'Spend',
    value: formatUsd(run.costUsd, precisionOf(run.costUsd)),
    detail: 'The run so far',
    hint: 'What the host says the run has cost, as its run index reports it',
  };
}

/** What a family of spans comes to: how many, and how many are in progress or failed. */
function countFigure(
  label: string,
  missing: string,
  spans: readonly {readonly lifecycle: Agent['lifecycle']; readonly failed: boolean}[] | null,
  noun: {readonly one: string; readonly many: string},
  /** Whether a span in progress is worth saying: a workstream is, a check that is running is not news. */
  showRunning = true,
): Figure {
  if (spans === null) return unreported(label, missing);
  if (spans.length === 0) return unreported(label, 'None yet');
  const running = spans.filter(span => span.lifecycle === 'running').length;
  const failed = spans.filter(span => span.failed).length;
  const parts = [
    showRunning && running > 0 ? `${formatCount(running)} in progress` : '',
    failed > 0 ? `${formatCount(failed)} failed` : '',
  ].filter(part => part !== '');
  return {
    label,
    value: formatCount(spans.length),
    detail: parts.length === 0 ? plural(spans.length, noun.one, noun.many) : parts.join(' · '),
  };
}

function agentsFigure(agents: readonly Agent[] | null, missing: string): Figure {
  if (agents === null) return unreported('Agents', missing);
  if (agents.length === 0) return unreported('Agents', 'None yet');
  const working = agents.filter(agent => agent.lifecycle === 'running').length;
  return {
    label: 'Agents',
    value: `${formatCount(working)} working`,
    detail: `of ${formatCount(agents.length)}`,
    hint: 'Agents with a turn in progress, of every agent the run has had',
  };
}

/** Every figure says its whole account on the pointer: a slot cuts a long value or detail short. */
const withHint = (figure: Figure): Figure => ({
  ...figure,
  hint:
    figure.hint ??
    [figure.label, [figure.value, figure.detail].filter(Boolean).join(', ')].join(': '),
});

function figuresOf(
  run: GenericRunEntry,
  detail: Presentation | null,
  missing: string,
): readonly Figure[] {
  return figuresFrom(run, detail, missing).map(withHint);
}

function figuresFrom(
  run: GenericRunEntry,
  detail: Presentation | null,
  missing: string,
): readonly Figure[] {
  return [
    bestFigure(run),
    agentsFigure(detail === null ? null : detail.agents.items, missing),
    countFigure(
      'Workstreams',
      missing,
      detail === null
        ? null
        : detail.workstreams.items.map(stream => ({
            lifecycle: stream.lifecycle,
            failed: workstreamResult(stream).failed || stream.lifecycle === 'failed',
          })),
      {one: 'workstream', many: 'workstreams'},
    ),
    countFigure(
      'Checks',
      missing,
      detail === null
        ? null
        : detail.correctness.items.map(gate => ({
            lifecycle: gate.lifecycle,
            failed: checkResult(gate).failed,
          })),
      {one: 'check', many: 'checks'},
      false,
    ),
    spendFigure(run),
  ];
}

function droppedWords(detail: Presentation): string | null {
  const {truncation} = detail.diagnostics;
  const kinds = [
    [truncation.agentEvictions, 'agent'],
    [truncation.workstreamEvictions, 'workstream'],
    [truncation.correctnessGateEvictions, 'check'],
    [truncation.metricSeriesEvictions, 'measure'],
  ] as const;
  const parts = kinds
    .filter(([count]) => count > 0)
    .map(([count, noun]) => `${formatCount(count)} ${plural(count, noun, `${noun}s`)}`);
  return parts.length === 0
    ? null
    : `The oldest were left out to stay within the app’s limits: ${parts.join(', ')}.`;
}

function unreadableWords(detail: Presentation): string | null {
  const count = detail.diagnostics.unprojectedSpans.items.length;
  return count === 0
    ? null
    : `${formatCount(count)} ${plural(count, 'record', 'records')} of the run ${plural(count, 'is', 'are')} in a form this app cannot show.`;
}

const EMPTY: SectionView<never> = {rows: [], note: null, empty: ''};

/** Why there is no detail to show: the first read is still going, or the host's answer could not be read. */
export type Absent = 'reading' | 'unread';

const ABSENT_WORDS: Readonly<Record<Absent, {readonly figure: string; readonly family: string}>> = {
  reading: {figure: 'Reading', family: 'Reading'},
  unread: {figure: 'Not read', family: 'Not read'},
};

/**
 * The overview of one run: the figures that tell it apart from the host's run index, and a section
 * for each family of typed facts. With no `detail` every section is empty and says why: the first
 * read is still going, or it could not be read (`absent`).
 */
export function overviewOf(
  run: GenericRunEntry,
  detail: Presentation | null,
  over: boolean,
  absent: Absent,
): GenericRunOverview {
  if (detail === null) {
    const words = ABSENT_WORDS[absent];
    const reading: SectionView<never> = {...EMPTY, empty: words.family};
    return {
      figures: figuresOf(run, null, words.figure),
      measures: reading,
      agents: reading,
      workstreams: reading,
      checks: reading,
      unreadable: null,
      dropped: null,
    };
  }
  const costs = detail.agents.items.flatMap(agent =>
    agent.usage?.costUsd === undefined ? [] : [agent.usage.costUsd],
  );
  const precision = precisionOf(costs.length === 0 ? null : costs.reduce((a, b) => a + b, 0));
  return {
    figures: figuresOf(run, detail, ''),
    measures: section(
      detail.optimization.metrics,
      measuresOf(detail.optimization.metrics.items),
      yet('No measurements', over),
    ),
    agents: section(
      detail.agents,
      detail.agents.items.map(agent => agentRow(agent, precision)),
      yet('No agents', over),
    ),
    workstreams: section(
      detail.workstreams,
      detail.workstreams.items.map(workstreamRow),
      yet('No workstreams', over),
    ),
    checks: section(
      detail.correctness,
      detail.correctness.items.map(checkRow),
      yet('No checks', over),
    ),
    unreadable: unreadableWords(detail),
    dropped: droppedWords(detail),
  };
}
