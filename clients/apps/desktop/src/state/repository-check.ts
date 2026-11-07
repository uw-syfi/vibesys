/**
 * What the Start form knows of the repository path as it is typed. The host says whether a start
 * could use a path (`CheckRepository`); the client keeps the host's last answer with the path it was
 * about, and reads it for the path now in the field. An answer about another path is no answer, so
 * a verdict is never the one for an earlier spelling. The rule behind a refusal is the host's: the
 * client only shows the words it was given.
 */

/** What the host said of one path. */
export type RepositoryAnswer =
  | {readonly _tag: 'Passed'}
  | {readonly _tag: 'Refused'; readonly reason: string}
  /**
   * The host could not say: it does not know the question (an older host), cannot launch agents
   * whatever the path, or the link failed. The path is then not held back by this check; the start
   * itself is still checked by the host.
   */
  | {readonly _tag: 'Unasked'};

/** The host's latest answer and the (trimmed) path it was about. */
export interface RepositoryChecked {
  readonly repository: string;
  readonly answer: RepositoryAnswer;
}

/** What the form shows and does for the path now in the field. */
export type RepositoryVerdict =
  | {readonly _tag: 'Empty'}
  | {readonly _tag: 'Checking'}
  | {readonly _tag: 'Valid'}
  | {readonly _tag: 'Invalid'; readonly reason: string};

/** The verdict for the path typed in the field, from the host's latest answer. */
export function repositoryVerdict(
  typed: string,
  checked: RepositoryChecked | undefined,
): RepositoryVerdict {
  const repository = typed.trim();
  if (repository === '') return {_tag: 'Empty'};
  if (checked === undefined || checked.repository !== repository) return {_tag: 'Checking'};
  return checked.answer._tag === 'Refused'
    ? {_tag: 'Invalid', reason: checked.answer.reason}
    : {_tag: 'Valid'};
}
