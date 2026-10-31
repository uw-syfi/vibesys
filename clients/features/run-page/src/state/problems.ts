/**
 * What the Run screen reports as problems: the failures a person passes through, as data with a
 * stable identity, for the app's board of problems. A state the page holds (the run ended, its
 * history is not available) stays on the page.
 */
import type {Problem} from '@vibesys/problems';
import type {RunPageState} from './run-page.js';

export function runPageProblems(state: RunPageState): readonly Problem[] {
  return state.notice === null
    ? []
    : [
        {
          id: 'run/command',
          tone: 'err',
          title: 'The run did not take the command',
          detail: state.notice,
        },
      ];
}
