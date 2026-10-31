/**
 * What the Run screen reports as problems: the failures a person passes through, as data with a
 * stable identity, for the app's board of problems. A state the page holds (the run ended, its
 * history is not available) is drawn on the page, in the header or the Logs.
 */
import type {Problem} from '@vibesys/problems';
import type {RunPageState} from './run-page.js';

export function runPageProblems(state: RunPageState): readonly Problem[] {
  const {notice, resumeStatus} = state;
  return [
    ...(notice === null
      ? []
      : [
          {
            id: 'run/command',
            tone: 'err' as const,
            title: 'The run did not take the command',
            detail: notice,
          },
        ]),
    ...(resumeStatus === null || resumeStatus.pending
      ? []
      : [
          {
            id: 'run/resume',
            tone: resumeStatus.tone,
            title: resumeStatus.title,
            detail: resumeStatus.detail,
          },
        ]),
  ];
}
