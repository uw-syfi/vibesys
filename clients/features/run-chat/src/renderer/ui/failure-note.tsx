import {Collapsible, ErrorActions, LogPane, Row, Text} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {type ChatFailure, DETAILS_LABEL} from '../../state/index.js';

/**
 * A failure's message, and its diagnostic under a disclosure that starts closed. With `ask`, it
 * also offers to ask the chat about it.
 */
export function FailureNote(props: {
  readonly failure: ChatFailure;
  readonly ask?: () => void;
}): JSX.Element {
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
      {props.ask === undefined ? null : (
        <Row gap="space-2">
          <ErrorActions ask={props.ask} />
        </Row>
      )}
    </>
  );
}
