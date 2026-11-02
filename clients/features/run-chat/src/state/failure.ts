/** What a failed call says to a person, and apart from it what it adds for diagnosis. */
export interface ChatFailure {
  readonly message: string;
  /** The host's diagnostic (its stderr); null when there is none. */
  readonly detail: string | null;
}

/** A call that threw: an `Error` says its message; anything else is shown as text. */
export function thrownFailure(error: unknown): ChatFailure {
  return {message: error instanceof Error ? error.message : String(error), detail: null};
}
