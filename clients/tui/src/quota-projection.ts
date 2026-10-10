import type {QuotaPause} from '@vibesys/core-state';

/** The error banner id a quota pause is shown under, so it can retire with the pause. */
export const QUOTA_BANNER_ID = 'quota_paused';

/** The slash command text that resumes on the configured fallback. */
export const RESUME_FALLBACK_ARGUMENT = 'fallback';

/** What the banner says happened: the provider and why, in the operator's words. */
export function quotaSummary(pause: QuotaPause): string {
  const why = pause.condition === 'quota_exhausted' ? 'is out of quota' : 'is rate limited';
  return `${pause.provider} ${why}. The run is paused until you decide.`;
}

/** The provider's own diagnostic, with the reset time when the provider gave one. */
export function quotaDetail(pause: QuotaPause): string {
  const reset =
    pause.resetsAt === null ? '' : ` Capacity returns at ${formatEpoch(pause.resetsAt)}.`;
  return `${pause.detail}${reset}`.trim();
}

/**
 * The operator's choices, in the order they are likely wanted.
 *
 * Waiting is the paused state itself, so it is listed as the default rather
 * than as a command. `/resume fallback` is offered only when the run names a
 * fallback: the backend ignores it otherwise, and a choice that does nothing
 * should not be on screen.
 */
export function quotaChoices(pause: QuotaPause): string {
  const choices: string[] = [];
  if (pause.resumesAt === null) {
    choices.push('Wait: the run stays paused.');
  } else {
    choices.push(`Wait: the run resumes by itself at ${formatEpoch(pause.resumesAt)}.`);
  }
  choices.push('/resume: try the same provider again.');
  if (pause.fallback !== null) {
    const model = pause.fallback.model === null ? '' : ` (${pause.fallback.model})`;
    choices.push(
      `/resume ${RESUME_FALLBACK_ARGUMENT}: continue on ${pause.fallback.provider}${model}; the turn in flight ends.`,
    );
  }
  return choices.join('\n');
}

/** UTC, so the same event reads the same on every machine. */
function formatEpoch(seconds: number): string {
  return `${new Date(seconds * 1000).toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}
