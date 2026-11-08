/** The words of a run's setup: labels, status words, figures. Nothing here explains; it names. */
import {RunStatus, SetupCheckId, type TaskCard} from '@vibesys/orchestrator-proto';
import type {Baseline, CheckState} from '../../../state/index.js';
import {span} from '../../duration.js';
import {formatFigure} from '../../figures.js';
import {runWord, type Word} from '../words.js';

/** What each of the six checks is called. */
export const CHECK_LABEL: Readonly<Record<SetupCheckId, string>> = {
  [SetupCheckId.UNSPECIFIED]: 'Check',
  [SetupCheckId.OBJECTIVE]: 'Objective',
  [SetupCheckId.CORRECTNESS_CHECK]: 'Correctness check',
  [SetupCheckId.BENCHMARK]: 'Benchmark',
  [SetupCheckId.BASE_PASSES]: 'Base code passes',
  [SetupCheckId.BROKEN_FAILS]: 'Broken code fails',
  [SetupCheckId.NUMBER_STABLE]: 'Number is stable',
};

/** The status word of a check, in the order of the states so the widest sets the slot. */
export const CHECK_WORD: Readonly<Record<CheckState, Word>> = {
  pending: {label: 'Pending', tone: 'neutral'},
  running: {label: 'Running', tone: 'live'},
  passed: {label: 'Passed', tone: 'ok'},
  failed: {label: 'Failed', tone: 'err'},
};

/** The run state of a run in setup, in the Runs list and on its page. */
const SETTING_UP: Word = {label: 'Setting up', tone: 'live'};

/** The run state while Start waits for the lead to finish its turn. */
export const STARTING: Word = {label: 'Starting', tone: 'live'};

/** The state word of a read that failed: the facts shown are the last ones read. */
export const AS_OF_LAST_READ = 'As of last read';

const lastName = (path: string): string =>
  path
    .split('/')
    .filter(part => part !== '')
    .at(-1) ?? path;

/** What a run in setup is called: the state and the repository's folder name, as no task is made yet. */
export const setupTitle = (repository: string): string => `Setting up · ${lastName(repository)}`;

/** The baseline as a figure with its unit and, when the host measured one, its spread. */
export function baselineWords(baseline: Baseline): string {
  const figure = `${formatFigure(baseline.value)}${baseline.unit === '' ? '' : ` ${baseline.unit}`}`;
  return baseline.spreadPercent === null
    ? figure
    : `${figure} · ${formatFigure(baseline.spreadPercent)}%`;
}

/** The objectives a card names, as `ops/s` or `latency (ms)` for each. */
export function objectiveWords(card: TaskCard): string {
  return card.objectives
    .map(objective =>
      objective.unit === '' ? objective.name : `${objective.name} (${objective.unit})`,
    )
    .join(', ');
}

/** Setup spend, in dollars. */
export const spendWords = (usd: number): string => `$${usd.toFixed(2)}`;

/** Setup time, in the words every screen shares. */
export const timeWords = (seconds: number): string => span(seconds * 1000);

/** The state word of a run that is not over: Setting up in its setup phase, else the run's own. */
export const runStateWord = (setup: boolean, status: RunStatus): Word =>
  setup && (status === RunStatus.STARTING || status === RunStatus.RUNNING)
    ? SETTING_UP
    : runWord(status);
