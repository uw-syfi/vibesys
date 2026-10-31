/**
 * What the Machines store reports as problems: the failures a person passes through, as data with
 * a stable identity, for the app's board of problems.
 */
import type {Problem} from '@vibesys/problems';
import type {MachinesState} from './machines.js';

export function machinesProblems(state: MachinesState): readonly Problem[] {
  return [
    ...(state.listProblem === null
      ? []
      : [
          {
            id: 'machines/list',
            tone: 'err' as const,
            title: 'The machines could not be read',
            detail: state.listProblem,
          },
        ]),
    ...(state.actionProblem === null
      ? []
      : [{id: 'machines/action', tone: 'err' as const, title: state.actionProblem}]),
  ];
}
