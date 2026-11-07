import {Text} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';

/** How many characters of a commit id the app shows; the full id is the text on hover. */
export const SHORT_COMMIT_LENGTH = 8;

const FULL_COMMIT = /\b[0-9a-f]{40}\b/g;

/** A commit id as the app shows it. */
export const shortCommit = (commit: string): string => commit.slice(0, SHORT_COMMIT_LENGTH);

/**
 * Text with each full commit id in it (40 hex digits) cut to its short form, in the code style and
 * with the full id on hover. Words the lead wrote stay as they are.
 */
export function withShortCommits(text: string): readonly ReactNode[] {
  const parts: ReactNode[] = [];
  let from = 0;
  for (const match of text.matchAll(FULL_COMMIT)) {
    if (match.index > from) parts.push(text.slice(from, match.index));
    parts.push(
      <Text key={match.index} variant="code" title={match[0]}>
        {shortCommit(match[0])}
      </Text>,
    );
    from = match.index + match[0].length;
  }
  if (from < text.length) parts.push(text.slice(from));
  return parts;
}

/** `withShortCommits` as a fragment, for a slot that takes one child. */
export function ShortCommits(props: {readonly text: string}): JSX.Element {
  return <>{withShortCommits(props.text)}</>;
}
