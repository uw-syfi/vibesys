import {Ellipsis, PanelLeft, PanelRight, Pause, Play} from 'lucide-react';
import {type ReactNode, type RefObject, useEffect, useRef} from 'react';
import type {RunControl} from '../model.js';
import type {RetainedText, StatusLine} from '../rounds.js';
import type {Menu} from '../ui-state.js';

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
      {project === null || project === title ? null : <span className="proj">{project}</span>}
      <span className="sp" />
      {children}
    </header>
  );
}

/** What the run is doing; an ended run that failed also says why, the full text in the hint. */
export function RunStatus({line, control}: {line: StatusLine; control: RunControl}) {
  if (control.kind === 'ended' && control.summary !== null) {
    return (
      <span className="status ended" aria-live="polite" title={control.tip ?? control.summary}>
        <span className="dot err" aria-hidden />
        <span className="why">
          {line.text}: {control.summary}
        </span>
      </span>
    );
  }
  return (
    <span className="status" aria-live="polite">
      {line.busy ? <span className="spin" /> : null}
      {line.paused ? (
        <Pause size={12} strokeWidth={1.75} fill="currentColor" className="warn" aria-hidden />
      ) : null}
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

/** Pause or Resume. Hidden while a transition is pending: the status line says so instead. */
export function RunControlChip({
  control,
  busy,
  onToggle,
}: {
  control: RunControl;
  busy: boolean;
  onToggle: () => void;
}) {
  if (control.kind !== 'action' || busy || control.label === 'Pausing') return null;
  const Icon = control.action === 'pause' ? Pause : Play;
  return (
    <button
      type="button"
      className="chip"
      title={control.tip}
      disabled={control.disabled}
      onClick={onToggle}
    >
      <Icon size={12} strokeWidth={1.75} fill="currentColor" aria-hidden />
      {control.label}
    </button>
  );
}

/** A popover under the ••• button: Escape or a click outside closes it. */
function Popover({
  role,
  label,
  onClose,
  children,
}: {
  role: 'menu' | 'alertdialog';
  label: {'aria-label': string} | {'aria-labelledby': string};
  onClose: () => void;
  children: ReactNode;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    addEventListener('keydown', onKey);
    return () => removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <>
      <button type="button" className="popscrim" aria-hidden tabIndex={-1} onClick={onClose} />
      <div className="pop" role={role} {...label}>
        {children}
      </div>
    </>
  );
}

/**
 * The Stop confirmation, anchored under the ••• button so the transcript stays readable. Cancel
 * takes focus first, and focus returns to the ••• button whichever way it closes.
 */
function StopConfirm({
  who,
  trigger,
  onCancel,
  onStop,
}: {
  who: string;
  trigger: RefObject<HTMLButtonElement | null>;
  onCancel: () => void;
  onStop: () => void;
}) {
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancel.current?.focus();
    return () => trigger.current?.focus();
  }, [trigger]);
  return (
    <Popover role="alertdialog" label={{'aria-labelledby': 'stop-title'}} onClose={onCancel}>
      <div className="confirm">
        <h4 id="stop-title">Stop this run?</h4>
        <p>
          The {who} finishes its call, then no further rounds start. Kept checkpoints stay in the
          repository.
        </p>
        <div className="row">
          <button ref={cancel} type="button" className="btn ghost" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="btn danger" onClick={onStop}>
            Stop run
          </button>
        </div>
      </div>
    </Popover>
  );
}

export interface MoreMenuProps {
  menu: Menu;
  canStop: boolean;
  /** Who finishes the current call before the run stops: `judge` or `current agent`. */
  stopWho: string;
  runId: string | null;
  onMenu: (menu: Menu) => void;
  onStop: () => void;
  /** Menu items placed before Copy run ID. */
  children?: ReactNode;
}

export function MoreMenu({menu, canStop, stopWho, runId, onMenu, onStop, children}: MoreMenuProps) {
  const trigger = useRef<HTMLButtonElement>(null);
  // A confirmation for a run that can no longer stop is gone, whatever the stored menu says.
  const shown = menu === 'stop' && !canStop ? null : menu;
  const close = () => onMenu(null);
  // Whatever closed the menu (an item, Escape, a click outside), focus goes back to •••
  // unless the item moved it on purpose.
  const wasOpen = useRef(false);
  useEffect(() => {
    if (wasOpen.current && shown === null && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    wasOpen.current = shown !== null;
  }, [shown]);
  return (
    <span className="menuwrap">
      <button
        ref={trigger}
        type="button"
        className={shown === null ? 'iconbtn' : 'iconbtn on'}
        title={canStop ? 'Notes, copy run ID, stop the run' : 'Notes, copy run ID'}
        aria-label="More"
        aria-haspopup="menu"
        aria-expanded={shown === 'more'}
        onClick={() => onMenu(shown === null ? 'more' : null)}
      >
        <Ellipsis size={16} strokeWidth={1.5} aria-hidden />
      </button>
      {shown === 'more' ? (
        <Popover role="menu" label={{'aria-label': 'Run'}} onClose={close}>
          {children}
          <button
            type="button"
            role="menuitem"
            className="it"
            disabled={runId === null}
            onClick={() => {
              if (runId !== null) void navigator.clipboard.writeText(runId);
              close();
            }}
          >
            Copy run ID
          </button>
          {canStop ? (
            <>
              <div className="sepl" />
              <button
                type="button"
                role="menuitem"
                className="it danger"
                onClick={() => onMenu('stop')}
              >
                Stop run…
              </button>
            </>
          ) : null}
        </Popover>
      ) : null}
      {shown === 'stop' ? (
        <StopConfirm who={stopWho} trigger={trigger} onCancel={close} onStop={onStop} />
      ) : null}
    </span>
  );
}

export function SidebarToggle({shown, onToggle}: {shown: boolean; onToggle: () => void}) {
  const label = shown ? 'Hide sidebar' : 'Show sidebar';
  return (
    <button type="button" className="iconbtn" title={label} aria-label={label} onClick={onToggle}>
      <PanelLeft size={16} strokeWidth={1.5} aria-hidden />
    </button>
  );
}

export function PaneToggle({open, onToggle}: {open: boolean; onToggle: () => void}) {
  return (
    <button
      type="button"
      className={open ? 'iconbtn on' : 'iconbtn'}
      title="Ask, changes, agents, experiments, notes"
      aria-label="Toggle side pane"
      aria-pressed={open}
      onClick={onToggle}
    >
      <PanelRight size={16} strokeWidth={1.5} aria-hidden />
    </button>
  );
}
