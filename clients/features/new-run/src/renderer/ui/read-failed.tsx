/**
 * A read that failed, said where what it would have shown goes (the project's tasks, its task's
 * goal, the agents the machine runs): what could not be read, the machine's own words for why,
 * and the ways on. The action row says only what holds the run back (`rowShowsProblem`).
 */
import {Button, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {NewRunContext, NewRunEvent, StepId} from '../../state/index.js';
import {problemWords} from './problem-words.js';
import {machineLabel} from './view.js';

/** Whether the page at `at` words the screen's problem itself, in place of what it could not read. */
function pageShowsProblem(
  context: NewRunContext,
  at: StepId | null,
  examplesFailed: boolean,
): boolean {
  switch (context.problem?.step) {
    case 'tasks':
      return at === 'project' || at === 'task';
    case 'goal':
      return at === 'goal' && context.answers.from === 'project';
    case 'harnesses':
      return at === 'agent' && context.harnesses?.for !== context.form.machine;
    case 'examples':
      return examplesFailed;
    default:
      return false;
  }
}

/**
 * Whether the action row shows the screen's problem: not when the page says it, and not for the
 * goal, which is read to be shown on its page and holds back no step and no start.
 */
export function rowShowsProblem(
  context: NewRunContext,
  at: StepId | null,
  examplesFailed: boolean,
): boolean {
  if (context.problem === null || context.problem.step === 'goal') return false;
  return !pageShowsProblem(context, at, examplesFailed);
}

export function ReadFailed(props: {
  readonly context: NewRunContext;
  readonly dispatch: (event: NewRunEvent) => void;
  /** Another way on than reading again, such as choosing another project. */
  readonly other?: {readonly text: string; readonly press: () => void};
}): JSX.Element | null {
  const {context, dispatch, other} = props;
  if (context.problem === null) return null;
  const words = problemWords(context.problem, machineLabel(context.form.machine));
  return (
    <Stack gap="space-3">
      <Stack gap="space-1">
        <Text variant="body-strong">{words.title}</Text>
        <Text tone="muted">{words.body}</Text>
        {words.detail === null ? null : (
          <Text variant="code" tone="muted">
            {words.detail}
          </Text>
        )}
      </Stack>
      {other === undefined && !words.retry ? null : (
        <Row gap="space-3">
          {other === undefined ? null : <Button text={other.text} press={other.press} />}
          {words.retry ? <Button text="Try again" press={() => dispatch({type: 'retry'})} /> : null}
        </Row>
      )}
    </Stack>
  );
}
