import {LoaderCircle, Pause, Play} from 'lucide-react';
import {titleCase} from '../derive.js';
import type {HeaderModel} from '../model.js';
import {Elapsed} from './Elapsed.js';
import './Header.css';

export interface HeaderProps {
  model: HeaderModel;
  /** "Pause failed: …" or "Resume failed: …", shown next to the control until the next command. */
  error: string | null;
  onControl: () => void;
}

const icon = {size: 16, strokeWidth: 1.75, 'aria-hidden': true} as const;

export function Header({model, error, onControl}: HeaderProps) {
  const {project, objective, startedAt, endedAt, usage, control} = model;
  const state = runState(model);
  return (
    <header className="hdr">
      <a className="brand" href="/">
        <span className="sr-only">VibeSys, all runs</span>
        <svg className="logo" viewBox="0 0 24 24" aria-hidden="true">
          <rect x="3" y="4" width="18" height="16" rx="1" />
          <path d="M7 9l3 3-3 3M13 15h4" />
        </svg>
      </a>
      <div className="title">
        {project === null ? null : <span className="proj mono">{project}</span>}
        {objective === null ? (
          <h1 className="obj">Run</h1>
        ) : (
          // biome-ignore lint/a11y/noNoninteractiveTabindex: focus shows the full objective in the tooltip.
          <h1 className="obj" tabIndex={0} data-tip={objective.full}>
            {objective.first}
          </h1>
        )}
      </div>
      <span
        className={`state st-${state.tone}`}
        tabIndex={control.kind === 'ended' && control.tip ? 0 : undefined}
        data-tip={control.kind === 'ended' ? (control.tip ?? undefined) : undefined}
      >
        <span className="dot" aria-hidden="true" />
        {state.word}
      </span>
      <p className="meta mono">
        {startedAt === null ? null : (
          <Elapsed
            className="elapsed"
            tip="Run elapsed"
            side={null}
            live={endedAt === null}
            ms={now =>
              (endedAt === null ? now.getTime() : Date.parse(endedAt)) - Date.parse(startedAt)
            }
          />
        )}
        {usage === null ? null : (
          // biome-ignore lint/a11y/noNoninteractiveTabindex: focus shows the tooltip that names it.
          <span className="ctx" tabIndex={0} data-tip="Context the last agent call carried">
            {usage}
          </span>
        )}
      </p>
      {error === null ? null : (
        <p className="ctl-error" role="alert">
          {error}
        </p>
      )}
      <div className="ctl">
        {control.kind === 'ended' ? null : (
          <button
            type="button"
            className="btn run-ctl"
            aria-label={control.label}
            aria-disabled={control.disabled || undefined}
            aria-keyshortcuts="P"
            data-tip={control.tip}
            data-key="P"
            onClick={() => {
              if (!control.disabled) onControl();
            }}
          >
            {control.label === 'Pause' ? (
              <Pause {...icon} />
            ) : control.label === 'Pausing' ? (
              <LoaderCircle {...icon} className="spin" />
            ) : (
              <Play {...icon} />
            )}
            <span>{control.label}</span>
          </button>
        )}
      </div>
    </header>
  );
}

/** The run's state as a word and a tone: the ended word once the run is over, else its status. */
function runState(model: HeaderModel): {word: string; tone: string} {
  const {control, status} = model;
  if (control.kind === 'ended') {
    return {word: control.word, tone: control.word === 'Completed' ? 'done' : 'failed'};
  }
  const tone =
    status === 'running'
      ? 'running'
      : status === 'pausing' || status === 'paused'
        ? 'paused'
        : 'idle';
  return {word: titleCase(status), tone};
}
