/**
 * "Start a run": a project, its tasks (listed by the host), extra arguments, and Start. While the
 * start runs the form is read-only; Cancel (or Esc) then cancels the start ("Cancelling…").
 */
import type {JSX, ReactNode} from 'react';
import type {CompletionState} from '../state/completion.js';
import {type StartAction, type StartState, startable, type Tasks} from '../state/start.js';
import {PathInput, Sheet, type WelcomeSenders} from './common.js';

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

function summary(state: Form): string {
  const task = state.kind === 'starting' ? state.task : startable(state);
  if (task === null) return 'Pick a project, then one of its tasks.';
  const args = state.args.trim();
  const extra = args === '' ? '' : ` ${args}`;
  return `Runs vibesys --detach --task ${task}${extra} in ${state.project.trim()}. Agent runs cost tokens.`;
}

function status(state: Form): {text: string; error: boolean} {
  if (state.kind === 'starting') {
    return {text: `Starting ${state.task} on ${state.host.label}…`, error: false};
  }
  return {text: state.error ?? '', error: state.error !== null};
}

function StartForm(props: {
  readonly state: Form;
  readonly completion: CompletionState;
  readonly send: WelcomeSenders;
}): JSX.Element {
  const {state, send} = props;
  const start = send.start;
  const editing = state.kind === 'editing';
  const tasks = state.kind === 'editing' ? state.tasks : state.listed;
  const line = status(state);
  return (
    <>
      <h2>Start a run on {state.host.label}</h2>
      <div className="row">
        <PathInput
          autoFocus
          field="project"
          host={state.host.key}
          value={state.project}
          completion={props.completion}
          complete={send.completion}
          change={value => start({type: 'project', value})}
          disabled={!editing}
          offers={state.projects}
          placeholder="Project directory, e.g. /home/me/src/queue-rs"
          onKeyDown={event => {
            if (event.key === 'Enter') start({type: 'list-tasks'});
          }}
          onBlur={() => {
            if (tasks.kind === 'idle') start({type: 'list-tasks'});
          }}
        />
        <button type="button" disabled={!editing} onClick={() => start({type: 'list-tasks'})}>
          List tasks
        </button>
      </div>
      <div className="tasks">
        <TaskChoices tasks={tasks} enabled={editing} start={start} />
      </div>
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
        <button type="button" onClick={() => start({type: 'close'})}>
          Cancel
        </button>
        <button
          type="button"
          className="primary"
          disabled={startable(state) === null}
          onClick={() => start({type: 'start'})}
        >
          Start run
        </button>
      </div>
    </>
  );
}

export function StartSheet(props: {
  readonly state: StartState;
  readonly completion: CompletionState;
  /** False while a run fills the window: the sheet waits behind it. */
  readonly visible: boolean;
  readonly send: WelcomeSenders;
}): JSX.Element {
  const {state, send} = props;
  const start = send.start;
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
    body = <StartForm state={state} completion={props.completion} send={send} />;
  }
  return (
    <Sheet
      open={state.kind !== 'closed' && props.visible}
      label="Start a run"
      close={() => start({type: 'close'})}
    >
      {body}
    </Sheet>
  );
}
