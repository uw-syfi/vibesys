/**
 * The Goal row of Review: the opening paragraph of the task's `OBJECTIVE.md`, and behind "Read
 * OBJECTIVE.md" the whole file, safely rendered, with what the task optimizes. An example's goal
 * comes with the catalogue; the goal of a task in the user's own project is read from the machine.
 * A task with no objectives file shows its goal with no direction: none is made up.
 */
import {Button, Card, Markdown, Row, Stack, Text} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  chosenEntry,
  goalKey,
  goalSummary,
  type NewRunContext,
  offers,
  projectPath,
} from '../../state/index.js';
import {entriesIn, NEEDS_UPDATE} from './view.js';

const READING = 'Reading the goal';

function taskFile(task: string): string {
  return `.vibesys/tasks/${task}/OBJECTIVE.md`;
}

interface Objective {
  readonly metric: string;
  readonly direction: 'max' | 'min';
}

/** The task's goal as the row shows it: its task, its text, and its axes. */
interface Goal {
  readonly task: string;
  readonly goal: string;
  readonly goal_truncated: boolean;
  readonly objectives: readonly Objective[];
}

/** The goal of the task picked, or null while it is not known. */
function goalOf(context: NewRunContext): Goal | null {
  if (context.answers.from === 'example') {
    const entry = chosenEntry(context.answers, entriesIn(context));
    return entry?.details == null ? null : {...entry.details, task: entry.task};
  }
  const project = projectPath(context.form);
  const {task} = context.answers;
  if (project === null || task === null) return null;
  const read = context.goal;
  if (read === null || read.for !== goalKey(context.form.machine, project, task)) return null;
  return read.value.kind === 'offered' ? read.value.value : null;
}

/**
 * What stands in place of a goal that is not read: the machine's VibeSys that cannot read it (it
 * lacks the feature that reads it) needs an update; one not heard from yet is being asked.
 */
function unreadStatus(context: NewRunContext): string {
  const {helper, form} = context;
  const feature = context.answers.from === 'example' ? 'example_catalogue' : 'project_goal';
  if (helper?.for !== form.machine) return READING;
  if (!offers(helper, form.machine, feature)) return NEEDS_UPDATE;
  return feature === 'project_goal' && context.problem === null ? READING : 'Not available';
}

function Objectives(props: {readonly objectives: readonly Objective[]}): JSX.Element {
  return (
    <Stack gap="space-1">
      {props.objectives.map(objective => (
        <Text key={objective.metric} variant="small" tone="muted">
          {objective.direction === 'max' ? 'Higher is better: ' : 'Lower is better: '}
          <Text variant="code">{objective.metric}</Text>
        </Text>
      ))}
    </Stack>
  );
}

export function GoalRow(props: {readonly context: NewRunContext}): JSX.Element {
  const {context} = props;
  const [open, setOpen] = useState(false);
  const goal = goalOf(context);
  if (goal === null) return <Text tone="muted">{unreadStatus(context)}</Text>;
  const summary = goalSummary(goal.goal);
  return (
    <Stack gap="space-2">
      {summary === '' ? null : <Text>{summary}</Text>}
      <Objectives objectives={goal.objectives} />
      <Row>
        <Button
          variant="quiet"
          size="sm"
          text={open ? 'Hide OBJECTIVE.md' : 'Read OBJECTIVE.md'}
          expanded={open}
          hint={taskFile(goal.task)}
          press={() => setOpen(!open)}
        />
      </Row>
      {open ? (
        <Card>
          <Stack gap="space-3">
            <Text variant="code" tone="muted">
              {taskFile(goal.task)}
            </Text>
            <Markdown text={goal.goal} />
            {goal.goal_truncated ? (
              <Text variant="small" tone="muted">
                Shortened
              </Text>
            ) : null}
          </Stack>
        </Card>
      ) : null}
    </Stack>
  );
}
