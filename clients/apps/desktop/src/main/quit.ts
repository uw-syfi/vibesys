/** The app lifetime's independent deadline and its graceful cleanup. */
export interface QuitLifetime {
  readonly release: () => Promise<void>;
  readonly deadline: () => {readonly elapsed: Promise<void>; readonly cancel: () => void};
  readonly exit: () => void;
  readonly timedOut: () => void;
}

/** Exit when cleanup completes or the deadline expires, even if cleanup cannot be interrupted. */
export async function quit(lifetime: QuitLifetime): Promise<void> {
  const deadline = lifetime.deadline();
  try {
    const result = await Promise.race([
      lifetime.release().then(() => 'released' as const),
      deadline.elapsed.then(() => 'deadline' as const),
    ]);
    if (result === 'deadline') lifetime.timedOut();
  } finally {
    deadline.cancel();
    lifetime.exit();
  }
}
