import type {KeyboardEvent} from 'react';

const OPTION = '[role="option"]';

/**
 * The keys of a flat listbox, shared by every list of options: Up and Down move the focus (no
 * wrap), Home and End jump to the ends, Enter and Space select. Returns whether it handled the key.
 */
export function optionKeys(event: KeyboardEvent<HTMLElement>, select: () => void): boolean {
  const list = event.currentTarget.closest('[role="listbox"]');
  const options = list === null ? [] : [...list.querySelectorAll<HTMLElement>(OPTION)];
  const index = options.indexOf(event.currentTarget);
  const focus = (at: number): void => options[at]?.focus();
  switch (event.key) {
    case 'ArrowDown':
      focus(Math.min(options.length - 1, index + 1));
      break;
    case 'ArrowUp':
      focus(Math.max(0, index - 1));
      break;
    case 'Home':
      focus(0);
      break;
    case 'End':
      focus(options.length - 1);
      break;
    case 'Enter':
    case ' ':
      select();
      break;
    default:
      return false;
  }
  event.preventDefault();
  return true;
}
