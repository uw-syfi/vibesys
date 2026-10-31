import type {JSX} from 'react';
import {Button} from './button.js';
import {DetailsToggle} from './details-toggle.js';
import {withoutPaths} from './paths.js';
import {StatusDot} from './status.js';
import {Text} from './text.js';

/** How a toast reads: `warn` is something to know, `err` something that failed. */
export type ToastTone = 'warn' | 'err';

/** One toast of a stack: what the stack's owner holds for it. */
export interface ToastItem {
  /** Names the toast to the stack's callbacks; unique within the stack. */
  readonly id: string;
  readonly tone: ToastTone;
  readonly title: string;
  /** The longer account behind the title, shown when the toast is open. */
  readonly detail?: string;
  readonly open: boolean;
}

const TONE_WORDS: Record<ToastTone, string> = {warn: 'Warning', err: 'Error'};

/**
 * A small notification that stays until it is dismissed: a tone dot, a short title, and a dismiss
 * button. With a `detail` it also has an expand control, which shows the detail under the title;
 * one without has nothing to open. An error is announced at once (`alert`), a warning when the
 * reader is idle (`status`); the tone is never color alone, the dot is named by its word. A
 * filesystem path in the title or the detail is shown by its last name only (`withoutPaths`).
 */
export function Toast(props: {
  readonly tone: ToastTone;
  readonly title: string;
  readonly detail?: string;
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
  readonly dismiss: () => void;
}): JSX.Element {
  return (
    <div
      className="vs-toast"
      data-tone={props.tone}
      role={props.tone === 'err' ? 'alert' : 'status'}
    >
      <div className="vs-toast-main">
        <div className="vs-toast-head">
          <StatusDot tone={props.tone} label={TONE_WORDS[props.tone]} />
          <span className="vs-toast-title">{withoutPaths(props.title)}</span>
        </div>
        {props.detail !== undefined && props.open ? (
          <div className="vs-toast-detail">
            <Text tone="muted">{withoutPaths(props.detail)}</Text>
          </div>
        ) : null}
      </div>
      <div className="vs-toast-controls">
        {props.detail === undefined ? null : (
          <DetailsToggle open={props.open} toggle={props.toggle} />
        )}
        <Button variant="quiet" size="sm" icon="cross" label="Dismiss" press={props.dismiss} />
      </div>
    </div>
  );
}

/**
 * The window's toasts, in the order given (the newest first), at the top right below the title bar,
 * over the page and out of the way of its controls. The stack itself is always present, so a toast
 * added to it is announced; it holds no state: the caller owns which toasts there are and which
 * are open.
 */
export function ToastStack(props: {
  readonly label: string;
  readonly toasts: readonly ToastItem[];
  readonly toggle: (id: string, open: boolean) => void;
  readonly dismiss: (id: string) => void;
}): JSX.Element {
  return (
    <section className="vs-toast-stack" aria-label={props.label} aria-live="polite">
      {props.toasts.map(toast => (
        <Toast
          key={toast.id}
          tone={toast.tone}
          title={toast.title}
          {...(toast.detail === undefined ? {} : {detail: toast.detail})}
          open={toast.open}
          toggle={open => props.toggle(toast.id, open)}
          dismiss={() => props.dismiss(toast.id)}
        />
      ))}
    </section>
  );
}
