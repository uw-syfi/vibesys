/**
 * The Goal page: the task's `OBJECTIVE.md` as written, safely rendered, with what the task
 * optimizes and the facts of its benchmark beside it. A task with no objectives file shows its
 * goal with no direction: none is made up.
 */
import {Card, KeyValue, type KeyValueItem, Markdown, Split, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type CatalogueEntry,
  chosenEntry,
  exampleLabel,
  hardwareLabel,
  MAKER_NAMES,
} from '../../state/index.js';
import type {CardProps} from './card-props.js';
import {backendLabel} from './cause-words.js';
import {entriesIn} from './view.js';

function taskFile(task: string): string {
  return `.vibesys/tasks/${task}/OBJECTIVE.md`;
}

function Objectives(props: {readonly entry: CatalogueEntry}): JSX.Element {
  const objectives = props.entry.details?.objectives ?? [];
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

export function GoalPage(props: CardProps): JSX.Element {
  const {context} = props;
  const entry = chosenEntry(context.answers, entriesIn(context));
  const details = entry?.details ?? null;
  const task = context.form.task ?? entry?.task ?? 'default';
  if (entry === null || details === null) {
    return (
      <Card>
        <Stack gap="space-2">
          <Text>VibeSys reads this task's goal when the run starts. It is not shown here yet.</Text>
          <Text variant="code" tone="muted">
            {taskFile(task)}
          </Text>
        </Stack>
      </Card>
    );
  }
  return (
    <Split
      main={
        <Card>
          <Stack gap="space-3">
            <Text variant="code" tone="muted">
              {taskFile(entry.task)}
            </Text>
            <Markdown text={details.goal} />
            {details.goal_truncated ? (
              <Text variant="small" tone="muted">
                Shortened here. The whole goal is in the file.
              </Text>
            ) : null}
          </Stack>
        </Card>
      }
      aside={
        <>
          <Objectives entry={entry} />
          <Card section="Benchmark">
            <KeyValue items={facts(entry)} />
          </Card>
        </>
      }
    />
  );
}
