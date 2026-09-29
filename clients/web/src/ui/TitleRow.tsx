import {Pause} from 'lucide-react';
import type {ReactNode} from 'react';
import type {RetainedText, StatusLine} from '../rounds.js';

export interface TitleRowProps {
  title: string;
  /** The whole objective: the title's hint. */
  objective: string | null;
  project: string | null;
  /** Before the title: the Show sidebar button while the sidebar is hidden. */
  leading?: ReactNode;
  children: ReactNode;
}

/** The run's name on the left, its status and controls on the right. */
export function TitleRow({title, objective, project, leading, children}: TitleRowProps) {
  return (
    <header className="titlebar">
      {leading}
      <span className="name" title={objective ?? title}>
        {title}
      </span>
      {project === null ? null : <span className="proj">{project}</span>}
      <span className="sp" />
      {children}
    </header>
  );
}

export function RunStatus({line}: {line: StatusLine}) {
  return (
    <span className="status" aria-live="polite">
      {line.busy ? <span className="spin" /> : null}
      {line.paused ? <Pause size={12} strokeWidth={1.75} className="warn" aria-hidden /> : null}
      {line.text}
    </span>
  );
}

export function Retained({text}: {text: RetainedText}) {
  return (
    <span className="kept num" title={text.hint}>
      {text.label} <b>{text.value}</b>
      {text.unit === null ? null : <span className="unit"> {text.unit}</span>}
      {text.change === null ? null : (
        <>
          {' · '}
          <span className={text.change.tone}>{text.change.text}</span>
          <span className="lbl"> vs baseline</span>
        </>
      )}
    </span>
  );
}
