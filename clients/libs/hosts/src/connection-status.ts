/**
 * What the window shows about a connection: the host connection's phase as the views word it, plus
 * the one run fact the host cannot know on its own (the attached run ended).
 */
export type ConnectionStatus =
  | {readonly kind: 'connecting'}
  | {readonly kind: 'connected'}
  | {readonly kind: 'reconnecting'; readonly attempt: number}
  /** The backoff is spent; a wake, a network change, or the user retries. */
  | {readonly kind: 'offline'; readonly detail: string}
  | {readonly kind: 'auth-needed'; readonly detail: string}
  | {readonly kind: 'incompatible'; readonly detail: string}
  | {readonly kind: 'run-ended'}
  | {readonly kind: 'closed'}
  /** This window's own channel to the run was refused: the host's session ceiling is spent. */
  | {readonly kind: 'capacity'; readonly detail: string};
