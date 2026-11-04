/**
 * The primitives today's desktop and web views render, styled by `src/primitives.css` (appended to
 * `tokens.css`). Frozen: they take `className` and raw HTML attributes, which the components of the
 * `.` entry refuse, and are deleted with their last consumer when the redesign replaces those views.
 */
export {Button, type ButtonVariant} from './button.js';
export {Combobox} from './combobox.js';
export {Dialog} from './dialog.js';
export {StatusChip, type Tone} from './status-chip.js';
