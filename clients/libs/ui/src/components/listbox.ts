import type {KeyboardEvent} from 'react';
import type {Windowed} from './windowing.js';

/** The index of the option a key event came from, read from its position in the list. */
function optionIndex(event: KeyboardEvent<HTMLElement>): number | null {
  const option = (event.target as HTMLElement).closest('[role="option"]');
  const position = Number(option?.getAttribute('aria-posinset'));
  return Number.isInteger(position) && position > 0 ? position - 1 : null;
}

/**
 * The movement keys of a windowed listbox of `count` options, handled on the listbox: Up and Down
 * step, Page Up and Down move a screenful, Home and End jump to the ends, none wrapping. The
 * target is scrolled into the drawn window and then focused, so the keys reach every option, drawn
 * or not.
 */
export function navigationKeys(
  event: KeyboardEvent<HTMLElement>,
  window: Windowed,
  count: number,
): void {
  const index = optionIndex(event);
  if (index === null) return;
  const page = Math.max(1, window.last - window.first - 1);
  let target: number;
  switch (event.key) {
    case 'ArrowDown':
      target = index + 1;
      break;
    case 'ArrowUp':
      target = index - 1;
      break;
    case 'PageDown':
      target = index + page;
      break;
    case 'PageUp':
      target = index - page;
      break;
    case 'Home':
      target = 0;
      break;
    case 'End':
      target = count - 1;
      break;
    default:
      return;
  }
  event.preventDefault();
  window.reveal(Math.max(0, Math.min(count - 1, target)), row => row.focus());
}

/** Enter and Space pick the option that has the focus. */
export function pickKeys(event: KeyboardEvent<HTMLElement>, pick: () => void): void {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  event.preventDefault();
  pick();
}
