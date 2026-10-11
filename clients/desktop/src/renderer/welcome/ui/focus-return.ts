/** Minimal focus contract used after a modal closes across a list redraw. */
export interface FocusReturnTarget {
  readonly isConnected: boolean;
  focus(): void;
}

/** Focus the opener when it survives a redraw, otherwise its stable logical region. */
export function restoreLogicalFocus(
  opener: FocusReturnTarget | null,
  fallback: FocusReturnTarget | null,
): void {
  if (opener?.isConnected === true) opener.focus();
  else if (fallback?.isConnected === true) fallback.focus();
}
