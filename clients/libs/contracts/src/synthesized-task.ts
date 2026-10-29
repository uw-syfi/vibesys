/**
 * The one task of a project VibeSys makes from flags (an evaluator and a source, no `.vibesys`
 * directory of the user's). Nobody chose it, so no screen names a run by it. The backend writes it
 * as `SYNTHESIZED_TASK_NAME` (`src/vibesys/inputs/_synthesis.py`); a Python test holds the two
 * equal.
 */
export const SYNTHESIZED_TASK_NAME = 'default';

/** Whether `task` is the name VibeSys gave a project made from flags, not one a person chose. */
export function isSynthesizedTask(task: string | null | undefined): boolean {
  return task === SYNTHESIZED_TASK_NAME;
}
