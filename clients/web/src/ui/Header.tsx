import {LoaderCircle, Pause, Play} from 'lucide-react';
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
  const {project, objective, startedAt, endedAt, control} = model;
  return (
    <header className="hdr">
      <a className="brand" href="/">
        <span className="sr-only">VibeSys, all runs</span>
        <svg className="logo" viewBox="0 0 24 24" aria-hidden="true">
          <rect x="3" y="4" width="18" height="16" rx="1" />
          <path d="M7 9l3 3-3 3M13 15h4" />
        </svg>
      </a>
      {project === null ? null : <h1 className="proj">{project}</h1>}
      {objective === null ? (
        <span className="hdr-fill" />
      ) : (
        // biome-ignore lint/a11y/noNoninteractiveTabindex: focus shows the full objective in the tooltip.
        <p className="obj" tabIndex={0} data-tip={objective.full}>
          {objective.first}
        </p>
      )}
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
      {error === null ? null : (
        <p className="ctl-error" role="alert">
          {error}
        </p>
      )}
      <div className="ctl">
        {control.kind === 'ended' ? (
          <span
            className={control.word === 'Completed' ? 'ended st-kept' : 'ended st-failed'}
            data-tip={control.tip ?? undefined}
          >
            {control.word}
          </span>
        ) : (
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
