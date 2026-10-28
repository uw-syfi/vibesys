/**
 * The Runs dashboard's words: every figure, line and sentence the tiles and the screen say, from
 * the facts the tile fold hands them. Nothing here decides what a run is; it only words it.
 */

/** A multiple of the base with its unit: `1.42×`. */
export function ratioWord(ratio: number): string {
  return `${ratio.toFixed(2)}×`;
}

/** A span in its two largest units: `45s`, `52m`, `1h 05m`. */
function durationWord(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 60) return `${whole}s`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}

/** `1 idea`, `3 ideas`. */
function ideasWord(count: number): string {
  return count === 1 ? '1 idea' : `${count} ideas`;
}

/** `text` with its first letter capitalized, as a line starts. */
function capitalized(text: string): string {
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
}

/** What a tile's ratio headline is measured against. */
export const RATIO_CAPTION = 'best vs input';

/** What a measured headline is: its unit, when the host names one. */
export function measuredCaption(unit: string | null): string {
  return unit === null ? 'best' : `${unit}, best`;
}

/** What a tile's chart says to assistive technology. */
export function chartLabel(ideas: number): string {
  return `Best so far over ${ideasWord(ideas)}, against the input`;
}

/** The words of the chart key, in the order the key lists them. */
export const CHART_KEY = {label: 'Chart key', line: 'best so far', reference: 'input'} as const;

/** A working run's line: `3 ideas working · 7 tried`, `Running · 7 ideas tried`. */
export function workingLine(working: number, tried: number | null): string {
  if (working > 0) {
    return tried === null
      ? `${ideasWord(working)} working`
      : `${ideasWord(working)} working · ${tried} tried`;
  }
  return tried === null ? 'Running' : `Running · ${ideasWord(tried)} tried`;
}

/** A paused run's line: who paused it, and what it tried (`Paused by you · 2 ideas tried`). */
export function pausedLine(paused: string, tried: number | null): string {
  return tried === null
    ? capitalized(paused)
    : `${capitalized(paused)} · ${ideasWord(tried)} tried`;
}

/** An ended run's line: how it ended, and what it did in how long (`Finished · 6 ideas in 40m`). */
export function endedLine(ended: string, tried: number | null, seconds: number | null): string {
  const time = seconds === null ? null : durationWord(seconds);
  const did =
    tried === null ? time : time === null ? ideasWord(tried) : `${ideasWord(tried)} in ${time}`;
  return did === null ? capitalized(ended) : `${capitalized(ended)} · ${did}`;
}

/** `line`, with why the run's state cannot be read after it when that is so. */
export function withProblem(line: string, problem: string | null): string {
  return problem === null ? line : `${line} · ${problem}`;
}

/** What a card's pill says of a run that failed. */
export const FAILED_WORD = 'Failed';

/** What a card's pill says of a working run whose last ideas failed (`3 failed in a row`). */
export function failingWord(streak: number): string {
  return streak > 1 ? `${streak} failed in a row` : 'Failing';
}

/** What a card's pill says of a working run whose journal is silent (`Quiet 32m`). */
export function quietWord(silentSeconds: number | null): string {
  return silentSeconds === null ? 'Quiet' : `Quiet ${durationWord(silentSeconds)}`;
}

/**
 * Why a failed run needs its user when its record gives no reason: what its ideas came to.
 * `failed` is how many of them failed; `best` the best result, as a multiple of the input.
 */
export function failedWhy(ideas: number | null, failed: number, best: number | null): string {
  if (ideas === null) return 'The run failed, and its record does not say why.';
  if (ideas === 0) return 'The run failed before it tried an idea.';
  if (failed === ideas) {
    return ideas === 1
      ? 'Its one idea failed, so nothing beat the input.'
      : `All ${ideas} ideas failed, so nothing beat the input.`;
  }
  return best === null
    ? `The run failed after ${ideasWord(ideas)}.`
    : `The run failed after ${ideasWord(ideas)}; the best was ${ratioWord(best)}.`;
}

/** Why a working run whose last ideas failed needs its user. */
export function failingWhy(streak: number): string {
  return streak > 1
    ? `Still running, but its last ${streak} ideas failed.`
    : 'Still running, but its last idea failed.';
}

/** Why a working run whose journal is silent needs its user. */
export function quietWhy(silentSeconds: number | null): string {
  return silentSeconds === null
    ? 'Still running, but it has recorded nothing lately.'
    : `Still running, but it has recorded nothing for ${durationWord(silentSeconds)}.`;
}

/** `SandboxLifecycleError` as words: `Sandbox lifecycle error`. */
function classWords(name: string): string {
  return capitalized(name.replace(/([a-z0-9])([A-Z])/g, '$1 $2').toLowerCase());
}

const ERROR_CLASS = /^([A-Z][A-Za-z0-9]*(?:Error|Exception|Failure|Interrupted))\s*:\s*/;

/**
 * A reason the run's record gives, as a sentence: an error's class in words (`NestedRepositoryError:
 * ...` reads `Nested repository error: ...`), the rest as it is.
 */
export function reasonSentence(line: string): string {
  const named = ERROR_CLASS.exec(line);
  return named?.[1] === undefined
    ? capitalized(line)
    : `${classWords(named[1])}: ${line.slice(named[0].length)}`;
}

/** A card's facts: where and when it ran, what it tried, its best, and how long it took (when given). */
export function cardFacts(parts: {
  readonly facts: string;
  readonly ideas: number | null;
  readonly best: string | null;
  readonly seconds: number | null;
}): string {
  return [
    parts.facts,
    ...(parts.ideas === null ? [] : [ideasWord(parts.ideas)]),
    ...(parts.best === null ? [] : [`best ${parts.best}`]),
    ...(parts.seconds === null ? [] : [durationWord(parts.seconds)]),
  ].join(' · ');
}

/** What a card offers a run that has ended. */
export const RUN_AGAIN = 'Run again with changes';

/** The title of the group of runs that need their user. */
export const NEEDS_YOU_TITLE = 'Needs you';

/** How a run's status reads in the heading that counts them. */
export const STATUS_TITLES = {
  running: 'Running',
  paused: 'Paused',
  failed: 'Failed',
  finished: 'Finished',
  stopped: 'Stopped',
  unknown: 'Unknown',
} as const;

/** The heading of the runs grouped by status: each status and its count (`Running 2 · Paused 1`). */
export function statusHeading(
  counts: readonly {readonly title: string; readonly count: number}[],
): string {
  return counts.map(each => `${each.title} ${each.count}`).join(' · ');
}

/** The screen's Group by: its label and what it offers. */
export const GROUP_LABEL = 'Group by';
export const GROUP_WORDS = {status: 'Status', machine: 'Machine', task: 'Task'} as const;

/** What the screen says with no run to show, on every machine or on the one it is narrowed to. */
export function noRunsWord(machine: string | null): string {
  return machine === null ? 'No runs' : `No runs on ${machine}`;
}
