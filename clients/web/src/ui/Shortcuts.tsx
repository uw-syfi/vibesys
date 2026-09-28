import {X} from 'lucide-react';
import {useEffect, useRef, useState} from 'react';
import './Shortcuts.css';

const KEY = 'vibesys.web.single-key';

export interface ShortcutsProps {
  onNext: () => void;
  onPrevious: () => void;
  onToggleRun: () => void;
  onJumpToLive: () => void;
}

function stored(): boolean {
  try {
    return localStorage.getItem(KEY) !== 'off';
  } catch {
    return true;
  }
}

/**
 * Keyboard shortcuts and the `?` dialog listing them. `j`/`k`/`l`/`p`/`/` are single-key
 * shortcuts the dialog can turn off (WCAG 2.1.4); `?` always opens the dialog so they can be
 * turned back on. Arrow keys change the round only while focus is on the rail or on nothing,
 * and `j`/`k` never do while focus is in the log, where they belong to its row cursor.
 */
export function Shortcuts({onNext, onPrevious, onToggleRun, onJumpToLive}: ShortcutsProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [enabled, setEnabled] = useState(stored);
  const actions = useRef({onNext, onPrevious, onToggleRun, onJumpToLive});
  useEffect(() => {
    actions.current = {onNext, onPrevious, onToggleRun, onJumpToLive};
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
      const inLog = target?.closest('#log') != null;
      const onRail = target === null || target === document.body || target.closest('.rail');
      if (onRail && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
        event.preventDefault();
        if (event.key === 'ArrowDown') actions.current.onNext();
        else actions.current.onPrevious();
        return;
      }
      if (!enabled) return;
      // Caps Lock and Shift still count; Ctrl, Meta, and Alt returned above.
      const letter = event.key.toLowerCase();
      // Round movement yields to the log's row cursor rather than being unbound there.
      if (inLog && (letter === 'j' || letter === 'k')) return;
      if (letter === 'j') actions.current.onNext();
      else if (letter === 'k') actions.current.onPrevious();
      else if (letter === 'l') actions.current.onJumpToLive();
      else if (letter === 'p') actions.current.onToggleRun();
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
          {/* The arrows belong to the log's row cursor, and are listed there. Claiming them
              here too would leave the reader no way to tell which claim applies when. */}
          <div>
            <dt>
              <kbd>j</kbd>
            </dt>
            <dd>Next round</dd>
          </div>
          <div>
            <dt>
              <kbd>k</kbd>
            </dt>
            <dd>Previous round</dd>
          </div>
          <div>
            <dt>
              <kbd>↑</kbd> <kbd>↓</kbd>
            </dt>
            <dd>Move the cursor in the log</dd>
          </div>
          <div>
            <dt>
              <kbd>→</kbd> <kbd>Enter</kbd>
            </dt>
            <dd>Show a tool call's output, or open a fold</dd>
          </div>
          <div>
            <dt>
              <kbd>←</kbd>
            </dt>
            <dd>Clear that output, or close a fold</dd>
          </div>
          <div>
            <dt>
              <kbd>l</kbd>
            </dt>
            <dd>Jump to the latest rows</dd>
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
            <dd>Close a dialog, or clear the output</dd>
          </div>
        </dl>
        <label className="keys-toggle">
          <input
            type="checkbox"
            checked={enabled}
            onChange={event => toggle(event.target.checked)}
          />
          Single-key shortcuts (j, k, l, p, /)
        </label>
      </div>
    </dialog>
  );
}
