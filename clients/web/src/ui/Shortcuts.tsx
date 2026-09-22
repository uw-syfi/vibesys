import {X} from 'lucide-react';
import {useEffect, useRef, useState} from 'react';
import './Shortcuts.css';

const KEY = 'vibesys.web.single-key';

export interface ShortcutsProps {
  onNext: () => void;
  onPrevious: () => void;
  onToggleRun: () => void;
}

function stored(): boolean {
  try {
    return localStorage.getItem(KEY) !== 'off';
  } catch {
    return true;
  }
}

/**
 * Keyboard shortcuts and the `?` dialog listing them. `j`/`k`/`p`/`/` are single-key shortcuts
 * the dialog can turn off (WCAG 2.1.4); `?` always opens the dialog so they can be turned back on.
 * Arrow keys change the round only while focus is on the rail or on nothing.
 */
export function Shortcuts({onNext, onPrevious, onToggleRun}: ShortcutsProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [enabled, setEnabled] = useState(stored);
  const actions = useRef({onNext, onPrevious, onToggleRun});
  useEffect(() => {
    actions.current = {onNext, onPrevious, onToggleRun};
  });

  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
      if (event.key === '?') {
        event.preventDefault();
        dialog.current?.showModal();
        return;
      }
      if (document.querySelector('dialog[open]')) return;
      const onRail = target === null || target === document.body || target.closest('.rail');
      if (onRail && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
        event.preventDefault();
        if (event.key === 'ArrowDown') actions.current.onNext();
        else actions.current.onPrevious();
        return;
      }
      if (!enabled) return;
      if (event.key === 'j') actions.current.onNext();
      else if (event.key === 'k') actions.current.onPrevious();
      else if (event.key === 'p') actions.current.onToggleRun();
      else if (event.key === '/') document.getElementById('steer')?.focus();
      else return;
      event.preventDefault();
    };
    document.addEventListener('keydown', keydown);
    return () => document.removeEventListener('keydown', keydown);
  }, [enabled]);

  function toggle(next: boolean) {
    setEnabled(next);
    try {
      localStorage.setItem(KEY, next ? 'on' : 'off');
    } catch {
      // Private mode: the choice lasts for this page only.
    }
  }

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: the click is on the backdrop; Esc closes natively.
    <dialog
      ref={dialog}
      className="keys"
      aria-labelledby="keys-title"
      onClick={event => {
        if (event.target === event.currentTarget) event.currentTarget.close();
      }}
    >
      <div className="keys-body">
        <div className="keys-head">
          <h2 id="keys-title">Keyboard shortcuts</h2>
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            aria-label="Close"
            onClick={() => dialog.current?.close()}
          >
            <X size={16} strokeWidth={1.75} aria-hidden />
          </button>
        </div>
        <dl className="keys-list">
          <div>
            <dt>
              <kbd>j</kbd> <kbd>↓</kbd>
            </dt>
            <dd>Next round</dd>
          </div>
          <div>
            <dt>
              <kbd>k</kbd> <kbd>↑</kbd>
            </dt>
            <dd>Previous round</dd>
          </div>
          <div>
            <dt>
              <kbd>p</kbd>
            </dt>
            <dd>Pause or resume</dd>
          </div>
          <div>
            <dt>
              <kbd>/</kbd>
            </dt>
            <dd>Steer the run</dd>
          </div>
          <div>
            <dt>
              <kbd>?</kbd>
            </dt>
            <dd>Show shortcuts</dd>
          </div>
          <div>
            <dt>
              <kbd>Esc</kbd>
            </dt>
            <dd>Close a dialog or sheet</dd>
          </div>
        </dl>
        <label className="keys-toggle">
          <input
            type="checkbox"
            checked={enabled}
            onChange={event => toggle(event.target.checked)}
          />
          Single-key shortcuts (j, k, p, /)
        </label>
      </div>
    </dialog>
  );
}
