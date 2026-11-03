/**
 * The pages for the user's own project: which project (a recent one, or a folder), then its task.
 * A project whose tasks could not be read says so on both pages, where its tasks would be.
 */

import {type CompletionAction, type CompletionState, suggestedEntries} from '@vibesys/hosts';
import {Button, ChoiceTiles, Field, PathInput, Row, Select, Stack, Text} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {OTHER_PROJECT_WORDS, PROJECT_FOLDER_WORDS, USE_FOLDER_WORDS} from '../../state/index.js';
import type {CardProps} from './card-props.js';
import {ReadFailed} from './read-failed.js';
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
  // Each folder by its name, the projects first and named so.
  const completions = suggestedEntries(completion.state, 'project').map(each => ({
    value: each.value,
    label: `${each.name}/`,
    ...(each.fits ? {detail: PROJECT_FOLDER_WORDS} : {}),
  }));
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
      <Row gap="space-3" align="end">
        <Stack grow>
          <Field label={`Folder on ${machineLabel(machine)}`}>
            <PathInput
              value={draft}
              change={type}
              completions={completions}
              placeholder="~/project"
            />
          </Field>
        </Stack>
        <Button
          text={USE_FOLDER_WORDS}
          press={open}
          {...(draft.trim() === ''
            ? {disabledReason: 'Type or choose a folder first.', quietReason: true}
            : {})}
        />
      </Row>
    </Stack>
  );
}

export function ProjectPage(props: ProjectProps): JSX.Element {
  const {context} = props;
  const {project} = context.answers;
  const recents = recentChoices(context.recent);
  const chosen =
    project?.source === 'recent' ? `${context.form.machine}${KEY}${project.path}` : null;
  return (
    <Stack gap="space-5">
      {context.problem?.step === 'tasks' ? <ReadFailed {...props} /> : null}
      {recents.length === 0 ? null : (
        <Stack gap="space-3">
          <Text variant="section">Recent</Text>
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
              if (host !== undefined && path !== undefined)
                chooseProject(props, host, 'recent', path);
            }}
          />
        </Stack>
      )}
      <Folder {...props} />
    </Stack>
  );
}

export function TaskPage(props: CardProps): JSX.Element {
  const {context, dispatch} = props;
  const {answers} = context;
  const tasks =
    answers.project !== null && context.tasks?.for === answers.project.path
      ? context.tasks.list
      : null;
  if (context.problem?.step === 'tasks') {
    return (
      <ReadFailed
        {...props}
        other={{text: OTHER_PROJECT_WORDS, press: () => dispatch({type: 'go', step: 'project'})}}
      />
    );
  }
  if (tasks === null) return <Text tone="muted">Reading</Text>;
  if (tasks.length === 0) return <Text tone="warn">No tasks</Text>;
  return (
    <ChoiceTiles
      label="Task"
      layout="cards"
      options={tasks.map(task => ({value: task, label: task, mono: true}))}
      value={answers.task}
      change={task => dispatch({type: 'answer', change: {kind: 'task', task}})}
    />
  );
}
