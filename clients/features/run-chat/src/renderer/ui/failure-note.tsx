import {Collapsible, LogPane, Text} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {type ChatFailure, DETAILS_LABEL} from '../../state/index.js';

/** A failure's message, and its diagnostic under a disclosure that starts closed. */
export function FailureNote(props: {readonly failure: ChatFailure}): JSX.Element {
  const [open, setOpen] = useState(false);
  const {message, detail} = props.failure;
  return (
    <>
      <Text variant="small" tone="err">
        {message}
      </Text>
      {detail === null ? null : (
        <Collapsible label={DETAILS_LABEL} open={open} toggle={setOpen}>
          <LogPane
            label={DETAILS_LABEL}
            size="sm"
            start="top"
            lines={detail.split('\n').map(text => ({text, stream: 'err' as const}))}
          />
        </Collapsible>
      )}
    </>
  );
}
