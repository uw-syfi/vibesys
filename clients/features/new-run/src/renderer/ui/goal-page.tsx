/**
 * The Goal page: the task's `OBJECTIVE.md` as written, safely rendered, with what the task
 * optimizes beside it, and for an example the facts of its benchmark. The same page shows the
 * goal of a task in the user's own project, read from the machine. A task with no objectives file
 * shows its goal with no direction: none is made up.
 */
import {Card, KeyValue, type KeyValueItem, Markdown, Split, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type CatalogueEntry,
  chosenEntry,
  exampleLabel,
  goalKey,
  hardwareLabel,
  MAKER_NAMES,
  type NewRunContext,
  projectPath,
} from '../../state/index.js';
import type {CardProps} from './card-props.js';
import {backendLabel} from './cause-words.js';
import {entriesIn} from './view.js';

function taskFile(task: string): string {
  return `.vibesys/tasks/${task}/OBJECTIVE.md`;
}

interface Objective {
  readonly metric: string;
  readonly direction: 'max' | 'min';
}

/** The task's goal as the page shows it: where it is written, its text, and its axes. */
interface Goal {
  readonly task: string;
  readonly goal: string;
  readonly goal_truncated: boolean;
  readonly objectives: readonly Objective[];
}

function Objectives(props: {readonly objectives: readonly Objective[]}): JSX.Element {
  const {objectives} = props;
  return (
    <Card section="Optimizes">
      {objectives.length === 0 ? (
        <Text tone="muted">
          The goal names the headline metric. This task declares no objectives, so no direction is
          shown.
        </Text>
      ) : (
        <Stack gap="space-3">
          {objectives.map(objective => (
            <Stack key={objective.metric} gap="space-1">
              <Text variant="body-strong">
                {objective.direction === 'max' ? 'Higher is better' : 'Lower is better'}
              </Text>
              <Text variant="code">{objective.metric}</Text>
            </Stack>
          ))}
        </Stack>
      )}
    </Card>
  );
}

function facts(entry: CatalogueEntry): KeyValueItem[] {
  const details = entry.details;
  if (details === null) return [];
  return [
    {key: 'Example', value: exampleLabel(entry.example), mono: true},
    {key: 'Task', value: entry.task, mono: true},
    ...(details.model === null
      ? []
      : [{key: 'Model', value: `${details.model.name} (${MAKER_NAMES[details.model.maker]})`}]),
    ...(details.workload === null ? [] : [{key: 'Workload', value: details.workload.name}]),
    {key: 'Hardware', value: hardwareLabel(details.hardware)},
    ...(details.hardware.backend === null
      ? []
      : [{key: 'Backend', value: backendLabel(details.hardware.backend)}]),
  ];
}

function GoalText(props: {readonly goal: Goal}): JSX.Element {
  const {goal} = props;
  return (
    <Card>
      <Stack gap="space-3">
        <Text variant="code" tone="muted">
          {taskFile(goal.task)}
        </Text>
        <Markdown text={goal.goal} />
        {goal.goal_truncated ? (
          <Text variant="small" tone="muted">
            Shortened here. The whole goal is in the file.
          </Text>
        ) : null}
      </Stack>
    </Card>
  );
}

/** The goal read from the machine for the user's own task now, or null while there is none. */
function ownGoal(context: NewRunContext): Goal | null {
  const project = projectPath(context.form);
  const {task} = context.answers;
  if (context.answers.from !== 'project' || project === null || task === null) return null;
  const read = context.goal;
  if (read === null || read.for !== goalKey(context.form.machine, project, task)) return null;
  return read.value.kind === 'offered' ? read.value.value : null;
}

function Unread(props: {readonly task: string; readonly reading: boolean}): JSX.Element {
  return (
    <Card>
      <Stack gap="space-2">
        <Text>{props.reading ? 'Reading the goal' : 'Goal not shown here'}</Text>
        <Text variant="code" tone="muted">
          {taskFile(props.task)}
        </Text>
      </Stack>
    </Card>
  );
}

function OwnGoalPage(props: CardProps): JSX.Element {
  const {context} = props;
  const goal = ownGoal(context);
  const task = context.answers.task ?? 'default';
  if (goal === null) {
    // Reading while the helper can read it and has not answered or failed.
    const reading =
      context.helper?.features.includes('project_goal') === true && context.problem === null;
    return <Unread task={task} reading={reading} />;
  }
  return (
    <Split
      main={<GoalText goal={goal} />}
      aside={
        <>
          <Objectives objectives={goal.objectives} />
          <Card section="Task">
            <KeyValue
              items={[
                {key: 'Project', value: projectPath(context.form) ?? '', mono: true},
                {key: 'Task', value: goal.task, mono: true},
              ]}
            />
          </Card>
        </>
      }
    />
  );
}

export function GoalPage(props: CardProps): JSX.Element {
  const {context} = props;
  if (context.answers.from === 'project') return <OwnGoalPage {...props} />;
  const entry = chosenEntry(context.answers, entriesIn(context));
  const details = entry?.details ?? null;
  const task = context.form.task ?? entry?.task ?? 'default';
  if (entry === null || details === null) return <Unread task={task} reading={false} />;
  return (
    <Split
      main={<GoalText goal={{...details, task: entry.task}} />}
      aside={
        <>
          <Objectives objectives={details.objectives} />
          <Card section="Benchmark">
            <KeyValue items={facts(entry)} />
          </Card>
        </>
      }
    />
  );
}
