/**
 * The user's own projects on the pick page: the recent ones, a folder opened by path, and once a
 * project is chosen, its task.
 */

import {type CompletionAction, type CompletionState, suggestions} from '@vibesys/hosts';
import {Button, ChoiceTiles, Field, PathInput, Row, Select, Stack, Text} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {CardProps} from './card-props.js';
import {type MachineOption, machineLabel, recentChoices} from './view.js';

/** The folder field's path completion: the hosts' completion reducer's state and its actions. */
export interface FolderCompletion {
  readonly state: CompletionState;
  readonly dispatch: (action: CompletionAction) => void;
}

type ProjectProps = CardProps & {
  readonly machines: readonly MachineOption[];
  readonly completion: FolderCompletion;
};

/** The project on `host`, chosen: the form moves to the machine it is on first. */
function chooseProject(
  props: CardProps,
  host: string,
  source: 'recent' | 'folder',
  path: string,
): void {
  if (props.context.form.machine !== host) props.dispatch({type: 'machine', machine: host});
  props.dispatch({type: 'answer', change: {kind: 'project', project: {source, path}}});
}

const KEY = '\n';

function Folder(props: ProjectProps): JSX.Element {
  const {context, completion} = props;
  const {project} = context.answers;
  const [draft, setDraft] = useState(project?.source === 'folder' ? project.path : '');
  const machine = context.form.machine;
  const type = (value: string): void => {
    setDraft(value);
    completion.dispatch({type: 'input', field: 'project', host: machine, value});
  };
  const completions = suggestions(completion.state, 'project').map(value => ({value}));
  const open = (): void => {
    // A completion ends in `/`; the project is the folder itself.
    const path = draft.trim().replace(/(.)\/+$/, '$1');
    if (path !== '') chooseProject(props, machine, 'folder', path);
  };
  return (
    <Stack gap="space-3">
      <Text variant="section">Open a folder</Text>
      {props.machines.length < 2 ? null : (
        <Field label="Machine">
          <Select
            value={machine}
            options={props.machines}
            change={next => props.dispatch({type: 'machine', machine: next})}
          />
        </Field>
      )}
      <Field label={`Folder on ${machineLabel(machine)}`}>
        <PathInput value={draft} change={type} completions={completions} placeholder="~/project" />
      </Field>
      <Row>
        <Button
          text="Use this folder"
          press={open}
          {...(draft.trim() === ''
            ? {disabledReason: 'Type or choose a folder first.', quietReason: true}
            : {})}
        />
      </Row>
    </Stack>
  );
}

/** The tasks of the project chosen, to choose one; what is missing while there is none. */
function Tasks(props: CardProps): JSX.Element | null {
  const {context, dispatch} = props;
  const {answers} = context;
  if (answers.project === null) return null;
  const tasks = context.tasks?.for === answers.project.path ? context.tasks.list : null;
  return (
    <Stack gap="space-3">
      <Text variant="section">Task</Text>
      {tasks === null ? (
        <Text tone="muted">Reading</Text>
      ) : tasks.length === 0 ? (
        <Stack gap="space-1">
          <Text tone="warn">No task yet</Text>
          <Text variant="small" tone="muted">
            A run starts from a task under .vibesys/tasks/ in the project.
          </Text>
        </Stack>
      ) : (
        <ChoiceTiles
          label="Task"
          layout="cards"
          options={tasks.map(task => ({value: task, label: task, mono: true}))}
          value={answers.task}
          change={task => dispatch({type: 'answer', change: {kind: 'task', task}})}
        />
      )}
    </Stack>
  );
}

/**
 * The recent projects, the folder field when it was asked for (or when there is no recent one),
 * and the chosen project's task.
 */
export function ProjectsPane(props: ProjectProps & {readonly folder: boolean}): JSX.Element {
  const {context} = props;
  const {project} = context.answers;
  const recents = recentChoices(context.recent);
  const chosen =
    project?.source === 'recent' ? `${context.form.machine}${KEY}${project.path}` : null;
  return (
    <Stack gap="space-5">
      {recents.length === 0 ? null : (
        <ChoiceTiles
          label="Recent projects"
          layout="cards"
          options={recents.map(choice => ({
            value: `${choice.host}${KEY}${choice.path}`,
            label: choice.label,
            note: choice.path,
            meta: machineLabel(choice.host),
          }))}
          value={chosen}
          change={value => {
            const [host, path] = value.split(KEY);
            if (host !== undefined && path !== undefined) {
              chooseProject(props, host, 'recent', path);
            }
          }}
        />
      )}
      {props.folder || recents.length === 0 || project?.source === 'folder' ? (
        <Folder {...props} />
      ) : null}
      <Tasks {...props} />
    </Stack>
  );
}
