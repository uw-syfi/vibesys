import {Chip} from '@vibesys/ui';
import type {JSX} from 'react';
import type {HostStatus} from '../state/index.js';
import {hostStatusWord} from './host-status-words.js';

/** A host's one status as a chip: its word, in its tone, with the way to fix a refusal on hover. */
export function HostStatusChip(props: {
  readonly status: HostStatus;
  readonly even?: boolean;
  readonly clip?: boolean;
}): JSX.Element {
  const word = hostStatusWord(props.status);
  return (
    <Chip
      label={word.label}
      tone={word.tone}
      dot
      {...(props.even === true ? {even: true} : {})}
      {...(props.clip === true ? {clip: true} : {})}
      {...(word.hint === undefined ? {} : {hint: word.hint})}
    />
  );
}
