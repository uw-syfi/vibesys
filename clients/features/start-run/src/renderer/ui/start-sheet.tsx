/**
 * "Start a run": a project, its tasks (listed by the host), the model (one of the host's vetted
 * pairs, or the project default; hidden when the host cannot take a choice), extra arguments, and
 * Start. While the start runs the form is read-only; Cancel (or Esc) then cancels the start
 * ("Cancelling…").
 */

import type {HostKey} from '@vibesys/hosts';
import {Button, Dialog} from '@vibesys/ui/legacy';
import type {InputHTMLAttributes, JSX, ReactNode} from 'react';
import {
  chosenHarness,
  type Harnesses,
  type StartAction,
  type StartState,
  startable,
  type Tasks,
} from '../../state/start.js';

/**
 * Renders the project field: a host path input the app supplies (another feature completes paths
 * on the host), given the sheet's value, events, and input attributes. It is called, not mounted
 * as a component, so the field keeps its identity (and its focus) while the app rebuilds this
 * function on every render.
 */
export type ProjectField = (
  props: {
    readonly host: HostKey;
    readonly value: string;
    readonly change: (value: string) => void;
    /** Paths to offer besides the completions (recent projects). */
    readonly offers: readonly string[];
  } & Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'list'>,
) => ReactNode;

type Form = Extract<StartState, {kind: 'editing' | 'starting'}>;

function TaskChoices(props: {
  readonly tasks: Tasks;
  readonly enabled: boolean;
  readonly start: (action: StartAction) => void;
}): ReactNode {
  const tasks = props.tasks;
  switch (tasks.kind) {
    case 'idle':
      return null;
    case 'loading':
      return <span className="muted">Reading tasks…</span>;
    case 'failed':
      return <span className="error">{tasks.error}</span>;
    case 'listed':
      if (tasks.list.length === 0) {
        return <span className="warn">This project defines no tasks.</span>;
      }
      return tasks.list.map(task => (
        <label key={task}>
          <input
            type="radio"
            name="task"
            value={task}
            checked={task === tasks.picked}
            disabled={!props.enabled}
            onChange={() => props.start({type: 'pick', task})}
          />
          {task}
        </label>
      ));
  }
}

/** The value of the picker's "Project default" option; every other option is a list index. */
const PROJECT_DEFAULT = '';

function HarnessPicker(props: {
  readonly harnesses: Harnesses;
  readonly enabled: boolean;
  readonly start: (action: StartAction) => void;
}): ReactNode {
  const harnesses = props.harnesses;
  switch (harnesses.kind) {
    case 'unsupported':
      return null;
    case 'loading':
      return <span className="muted">Reading the vetted models…</span>;
    case 'failed':
      return (
        <span className="muted">
          Could not read the vetted models ({harnesses.error}); the project default applies.
        </span>
      );
    case 'listed': {
      const {list, picked} = harnesses;
      const index =
        picked === null
          ? -1
          : list.findIndex(
              option => option.provider === picked.provider && option.model === picked.model,
            );
      const providers = [...new Set(list.map(option => option.provider))];
      return (
        <label className="harness">
          Model
          <select
            aria-label="Model"
            value={index < 0 ? PROJECT_DEFAULT : String(index)}
            disabled={!props.enabled}
            onChange={event => {
              const option =
                event.target.value === PROJECT_DEFAULT
                  ? undefined
                  : list[Number(event.target.value)];
              const value =
                option === undefined ? null : {provider: option.provider, model: option.model};
              props.start({type: 'harness', value});
            }}
          >
            <option value={PROJECT_DEFAULT}>Project default (agent.toml)</option>
            {providers.map(provider => (
              <optgroup key={provider} label={provider}>
                {list.map((option, at) =>
                  option.provider === provider ? (
                    <option key={option.model} value={String(at)}>
                      {option.default ? `${option.model} (default)` : option.model}
                    </option>
                  ) : null,
                )}
              </optgroup>
            ))}
          </select>
        </label>
      );
    }
  }
}

function summary(state: Form): string {
  const task = state.kind === 'starting' ? state.task : startable(state);
  if (task === null) return 'Pick a project, then one of its tasks.';
  const harness = chosenHarness(state);
  const chosen =
    harness === null ? '' : ` --cli-provider ${harness.provider} --model ${harness.model}`;
  const args = state.args.trim();
  const extra = args === '' ? '' : ` ${args}`;
  return `Runs vibesys --detach --task ${task}${chosen}${extra} in ${state.project.trim()}. Agent runs cost tokens.`;
}

function status(state: Form): {text: string; error: boolean} {
  if (state.kind === 'starting') {
    return {text: `Starting ${state.task} on ${state.host.label}…`, error: false};
  }
  return {text: state.error ?? '', error: state.error !== null};
}

function StartForm(props: {
  readonly state: Form;
  readonly projectField: ProjectField;
  readonly start: (action: StartAction) => void;
}): JSX.Element {
  const {state, start, projectField} = props;
  const editing = state.kind === 'editing';
  const tasks = state.kind === 'editing' ? state.tasks : state.listed;
  const line = status(state);
  return (
    <>
      <h2>Start a run on {state.host.label}</h2>
      <div className="row">
        {projectField({
          autoFocus: true,
          host: state.host.key,
          value: state.project,
          change: value => start({type: 'project', value}),
          disabled: !editing,
          offers: state.projects,
          placeholder: 'Project directory, e.g. /home/me/src/queue-rs',
          onKeyDown: event => {
            if (event.key === 'Enter') start({type: 'list-tasks'});
          },
          onBlur: () => {
            if (tasks.kind === 'idle') start({type: 'list-tasks'});
          },
        })}
        <Button disabled={!editing} onClick={() => start({type: 'list-tasks'})}>
          List tasks
        </Button>
      </div>
      <div className="tasks">
        <TaskChoices tasks={tasks} enabled={editing} start={start} />
      </div>
      <HarnessPicker harnesses={state.harnesses} enabled={editing} start={start} />
      <div className="row">
        <input
          value={state.args}
          disabled={!editing}
          placeholder="Extra run arguments, quoted like a shell (optional)"
          spellCheck={false}
          onChange={event => start({type: 'args', value: event.target.value})}
        />
      </div>
      <p className="muted">{summary(state)}</p>
      <p className={line.error ? 'status error' : 'status'} role="status">
        {line.text}
      </p>
      <div className="actions">
        <Button onClick={() => start({type: 'close'})}>Cancel</Button>
        <Button
          variant="primary"
          disabled={startable(state) === null}
          onClick={() => start({type: 'start'})}
        >
          Start run
        </Button>
      </div>
    </>
  );
}

export function StartSheet(props: {
  readonly state: StartState;
  /** False while a run fills the window: the sheet waits behind it. */
  readonly visible: boolean;
  readonly projectField: ProjectField;
  readonly start: (action: StartAction) => void;
}): JSX.Element {
  const {state, start} = props;
  let body: ReactNode = null;
  if (state.kind === 'cancelling') {
    body = (
      <>
        <h2>Start a run on {state.host.label}</h2>
        <p className="status" role="status">
          Cancelling… The run of {state.task} is stopped as soon as it appears.
        </p>
      </>
    );
  } else if (state.kind !== 'closed') {
    body = <StartForm state={state} projectField={props.projectField} start={start} />;
  }
  return (
    <Dialog
      open={state.kind !== 'closed' && props.visible}
      label="Start a run"
      dismiss={() => start({type: 'close'})}
    >
      {body}
    </Dialog>
  );
}
