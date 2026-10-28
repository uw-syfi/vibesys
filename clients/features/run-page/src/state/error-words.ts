/**
 * The Run screen's errors as the run chat is asked about them: where each happened, what it said,
 * and its diagnostic. The banners read their words from the same selectors, so the question and
 * the banner never disagree.
 */

import type {RecordRow} from '@vibesys/run-state/records';
import type {RunPageState} from './run-page.js';
import {endedView} from './views.js';

/** An error handed to the run chat: where it happened, what it said, and its diagnostic. */
export interface ErrorReport {
  readonly where: string;
  readonly message: string;
  readonly detail: string | null;
}

/** What the ended banner says: its title and body; null for a run that has not ended. */
export interface EndedBannerWords {
  readonly title: string;
  readonly body: string;
}

const NO_REASON = 'The run’s record does not say why it ended.';

/**
 * The ended banner's words. A read-only (saved) run names its end by the record's lifecycle word
 * and the run's own reason; a run this window watched by the ending it saw.
 */
export function endedBannerWords(state: RunPageState): EndedBannerWords | null {
  const ended = endedView(state);
  if (ended === null) return null;
  if (state.readOnly === true)
    return {
      title: state.lifecycleWord ?? ended.title,
      body: (state.record.kind === 'ended' ? state.record.run.reason : null) ?? '',
    };
  return {title: ended.title, body: ended.reason};
}

/** The ended banner's error: null for Finished or a run that has not ended. */
export function endedErrorReport(state: RunPageState): ErrorReport | null {
  const words = endedBannerWords(state);
  if (words === null || endedView(state)?.title === 'Finished') return null;
  return {
    where: `Run ended: ${words.title}`,
    message: words.body === '' ? NO_REASON : words.body,
    detail: null,
  };
}

/**
 * The diagnostic the Run diagnostic banner shows, or null: none was recorded, or it repeats the
 * reason the ended banner already shows (one message per failure).
 */
export function shownDiagnostic(state: RunPageState): RecordRow | null {
  const row = state.recordDiagnostic;
  if (row === undefined) return null;
  const ended = endedBannerWords(state);
  return ended !== null && ended.body !== '' && row.message.includes(ended.body) ? null : row;
}

/**
 * The Run diagnostic banner's error, or null without one. Null too when the diagnostic is the
 * run's own ending, which the ended banner already asks about: one offer per error.
 */
export function diagnosticErrorReport(state: RunPageState): ErrorReport | null {
  const row = shownDiagnostic(state);
  if (row === null) return null;
  if (row.body === 'lifecycle' && endedErrorReport(state) !== null) return null;
  return {where: 'Run diagnostic', message: row.message, detail: `Record ${row.seq}: ${row.event}`};
}

/** The "The run did not take the command" banner's error, or null without a notice. */
export function commandErrorReport(state: RunPageState): ErrorReport | null {
  return state.notice === null ? null : {where: 'Run command', message: state.notice, detail: null};
}
