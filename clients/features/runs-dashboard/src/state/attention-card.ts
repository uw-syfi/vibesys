/**
 * The card of a run that needs its user: a pill that names the trouble, a sentence that says why,
 * the facts line and whether the run can be run again. Nothing here decides who needs their user
 * (the host does, and `attentionOf` reads it); it only words it.
 */
import {durationWord, ideasWord, ratioWord} from './words.js';

/** Why a card's run needs its user. */
export type CardAttention = 'failed' | 'repeated_failure' | 'stalled';

/** What a card says of its run. */
export interface AttentionCardView {
  readonly pill: {readonly tone: 'warn' | 'err'; readonly label: string};
  /** Why the run needs its user, in a sentence. */
  readonly why: string;
  /** Where and when it ran, what it tried, its best, and how long it took (an ended run). */
  readonly facts: string;
  /** Whether the card offers Run again: only a run that has ended can be. */
  readonly again: boolean;
}

/** What a card offers a run that has ended. */
export const RUN_AGAIN = 'Run again';

function capitalized(text: string): string {
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
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
function reasonSentence(line: string): string {
  const named = ERROR_CLASS.exec(line);
  return named?.[1] === undefined
    ? capitalized(line)
    : `${classWords(named[1])}: ${line.slice(named[0].length)}`;
}

/**
 * Why a failed run needs its user when its record gives no reason: what its ideas came to.
 * `allFailed` is how many ideas there were when every one of them failed (none otherwise), `ideas`
 * how many it tried, and `best` its best result as a multiple of the input.
 */
function failedWhy(parts: {
  readonly allFailed: number | null;
  readonly ideas: number | null;
  readonly best: number | null;
}): string {
  const {allFailed, ideas, best} = parts;
  const nothing = best === null ? ', so nothing beat the input' : '';
  if (allFailed !== null) {
    return allFailed === 1
      ? `Its one idea failed${nothing}.`
      : `All ${allFailed} ideas failed${nothing}.`;
  }
  if (ideas === null) return 'The run failed, and its record does not say why.';
  if (ideas === 0) return 'The run failed before it tried an idea.';
  return best === null
    ? `The run failed after ${ideasWord(ideas)}.`
    : `The run failed after ${ideasWord(ideas)}; the best was ${ratioWord(best)}.`;
}

/** The card of a run that needs its user (see the parts' meaning in `tileOf`). */
export function attentionCardOf(parts: {
  readonly attention: CardAttention;
  /** The run is no longer working: it can be run again and its duration is final. */
  readonly ended: boolean;
  /** The first line of why an ended run ended, when its record says. */
  readonly reason: string | null;
  readonly ideas: number | null;
  readonly best: number | null;
  readonly seconds: number | null;
  readonly allFailed: number | null;
  readonly failedStreak: number;
  readonly silentSeconds: number | null;
  /** The tile's facts line: machine, start and unit. */
  readonly facts: string;
}): AttentionCardView {
  const {attention, ended} = parts;
  const base = {
    facts: [
      parts.facts,
      ...(parts.ideas === null ? [] : [ideasWord(parts.ideas)]),
      ...(parts.best === null ? [] : [`best ${ratioWord(parts.best)}`]),
      ...(ended && parts.seconds !== null ? [durationWord(parts.seconds)] : []),
    ]
      .filter(part => part !== '')
      .join(' · '),
    again: ended,
  };
  switch (attention) {
    case 'failed':
      return {
        ...base,
        pill: {tone: 'err', label: 'Failed'},
        why: parts.reason === null ? failedWhy(parts) : reasonSentence(parts.reason),
      };
    case 'repeated_failure':
      return {
        ...base,
        pill: {
          tone: 'warn',
          label: parts.failedStreak > 1 ? `${parts.failedStreak} failed in a row` : 'Failing',
        },
        why:
          parts.failedStreak > 1
            ? `Still running, but its last ${parts.failedStreak} ideas failed.`
            : 'Still running, but its last idea failed.',
      };
    case 'stalled':
      return {
        ...base,
        pill: {
          tone: 'warn',
          label:
            parts.silentSeconds === null ? 'Quiet' : `Quiet ${durationWord(parts.silentSeconds)}`,
        },
        why:
          parts.silentSeconds === null
            ? 'Still running, but it has recorded nothing lately.'
            : `Still running, but it has recorded nothing for ${durationWord(parts.silentSeconds)}.`,
      };
  }
}
