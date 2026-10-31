/**
 * What the Machines store reports as problems: the failures a person passes through, as data with
 * a stable identity, for the app's board of problems.
 */
import type {Problem} from '@vibesys/problems';
import type {ActionProblem, MachinesState} from './machines.js';
import {ACTION_FAILURE} from './words.js';

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
    ...(state.actionProblem === null ? [] : [actionProblem(state.actionProblem)]),
  ];
}

/** A failed action: worded by what it was, with what the machine reported as its detail. */
function actionProblem(problem: ActionProblem): Problem {
  const {title} = ACTION_FAILURE[problem.kind];
  return {
    id: `machines/action/${problem.token}`,
    tone: 'err',
    title,
    ...(problem.error === title ? {} : {detail: problem.error}),
  };
}
