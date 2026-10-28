/**
 * The Run screen's panes a person can resize, and what each one's separator says.
 *
 * A role is remembered as one width (by the app, `PaneSizes`): `master` is the list at the left of
 * a level (the Ideas, an idea's steps, a step beside its item), `agents` the Agents list, `now`
 * what runs now (at the right, resized from its left edge), `wide` the Timeline beside the idea it
 * opened, and `chat` the chat panel. A role keeps its width at every level, so moving between
 * levels moves no pane.
 */

export type PaneRole = 'master' | 'agents' | 'now' | 'wide' | 'chat';
export const PANE_ROLES: readonly PaneRole[] = ['master', 'agents', 'now', 'wide', 'chat'];

/** The width a person gave each role, in pixels; a role without one keeps its default width. */
export type PaneWidths = Readonly<Partial<Record<PaneRole, number>>>;

/** The widths the screen draws its panes at, and where a new width a person gives one goes. */
export interface PaneSizes {
  readonly widths: PaneWidths;
  change(role: PaneRole, width: number): void;
}

/** The separator's accessible name beside each pane that has one. */
export const RESIZE_WORDS = {
  ideas: 'Resize the ideas list',
  timeline: 'Resize the timeline',
  steps: 'Resize the steps list',
  step: 'Resize the step',
  agents: 'Resize the agents list',
  now: 'Resize the running list',
  chat: 'Resize the chat',
} as const;

/** A pane's width as a screen reader says the separator's value. */
export const paneWidthWords = (width: number): string => `${width} pixels wide`;
