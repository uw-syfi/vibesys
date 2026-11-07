/** Which key presses mean "back", decided from plain facts about the press so a test can walk them. */

export interface KeyPress {
  readonly key: string;
  readonly alt: boolean;
  readonly ctrl: boolean;
  readonly meta: boolean;
  readonly shift: boolean;
  /** Something nearer the focus already took the press (it called `preventDefault`). */
  readonly handled: boolean;
  /**
   * The focus is where Escape and the arrows already mean something else: a text field, a list
   * of choices, an open menu or a dialog.
   */
  readonly capturing: boolean;
}

/** Escape, or Alt+Left, on its own and outside anything that uses them. */
export function isBack(press: KeyPress): boolean {
  if (press.handled || press.capturing) return false;
  const modifiers = [press.alt, press.ctrl, press.meta, press.shift].filter(Boolean).length;
  if (press.key === 'Escape') return modifiers === 0;
  return press.key === 'ArrowLeft' && press.alt && modifiers === 1;
}
