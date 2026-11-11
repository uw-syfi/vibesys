import {RunStatus} from '@vibesys/orchestrator-proto';
import type {CostWords} from '../cost/index.js';
import type {CostOverviewWords} from '../cost-overview/index.js';
import type {DisplayClock} from '../display-time.js';
import {faultWord, harnessWord, roleWord, runWord} from './words.js';

const DAY_MS = 86_400_000;

/** The run's vocabulary, handed to the Cost screen it shows; its moments are on `clock`. */
export const costWords = (clock: DisplayClock): CostWords => ({
  role: roleWord,
  harness: harnessWord,
  fault: faultWord,
  moment: clock.datedMoment24,
});

/**
 * The words of the aggregate Cost screen: the run's, with every moment told on `clock`. A finished
 * run is a neutral chip here: this page draws the models in the palette's greens, blues and oranges,
 * and a status that shared a hue with a series would read as one, so only what wants a look (a run
 * that is running, stopping or failed) keeps its tone.
 */
export const costOverviewWords = (clock: DisplayClock): CostOverviewWords => ({
  ...costWords(clock),
  run: status =>
    status === RunStatus.ENDED ? {label: runWord(status).label, tone: 'neutral'} : runWord(status),
  tick: clock.reportTick,
  offset: clock.offsetMs,
  started: atMs => clock.reportTick(atMs, 13 * DAY_MS, DAY_MS),
});
