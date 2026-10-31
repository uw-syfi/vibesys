import type {JSX} from 'react';
import {Button} from './button.js';
import {Collapsible} from './collapsible.js';
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
 * button. With a `detail` the title is also the control that opens it; a toast without one has
 * nothing to open. An error is announced at once (`alert`), a warning when the reader is idle
 * (`status`); the tone is never color alone, the dot is named by its word.
 */
export function Toast(props: {
  readonly tone: ToastTone;
  readonly title: string;
  readonly detail?: string;
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
  readonly dismiss: () => void;
}): JSX.Element {
  const dot = <StatusDot tone={props.tone} label={TONE_WORDS[props.tone]} />;
  return (
    <div
      className="vs-toast"
      data-tone={props.tone}
      role={props.tone === 'err' ? 'alert' : 'status'}
    >
      <div className="vs-toast-main">
        {props.detail === undefined ? (
          <div className="vs-toast-head" title={props.title}>
            {dot}
            <span className="vs-toast-title">{props.title}</span>
          </div>
        ) : (
          <Collapsible label={props.title} leading={dot} open={props.open} toggle={props.toggle}>
            <div className="vs-toast-detail">
              <Text tone="muted">{props.detail}</Text>
            </div>
          </Collapsible>
        )}
      </div>
      <Button variant="quiet" size="sm" icon="cross" label="Dismiss" press={props.dismiss} />
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
