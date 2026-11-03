/** The window's IPC identity is valid only until Electron destroys its contents. */
export interface WindowLifetime {
  readonly webContents: {readonly id: number};
  on(event: 'closed', listener: () => void): void;
}

/** Admit a live window and revoke it on close without reaching into a destroyed native object. */
export function trackWindow(windows: Set<number>, window: WindowLifetime): void {
  // Electron has already destroyed webContents when it emits closed. Retain its identity now.
  const id = window.webContents.id;
  windows.add(id);
  window.on('closed', () => windows.delete(id));
}
