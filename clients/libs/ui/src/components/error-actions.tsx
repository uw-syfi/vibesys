import {type JSX, useState} from 'react';
import {Button, type ButtonSize} from './button.js';

const COPY = 'Copy details';

/**
 * What a person can do with an error: hand it to an assistant (`ask`), or copy its text (`copy`).
 * The words are fixed here. A press of Copy details writes the text to the clipboard and says
 * whether that worked, until the text changes. `size` matches the buttons beside them.
 */
export function ErrorActions(props: {
  readonly ask?: () => void;
  readonly copy?: string;
  readonly size?: ButtonSize;
}): JSX.Element | null {
  const [copied, setCopied] = useState<{readonly text: string; readonly label: string} | null>(
    null,
  );
  const {ask, copy} = props;
  const size = props.size ?? 'md';
  if (ask === undefined && copy === undefined) return null;
  const press = (text: string): void => {
    // A user-pressed platform action, like focus: the clipboard belongs to the page.
    Promise.resolve()
      .then(() => navigator.clipboard.writeText(text))
      .then(
        () => setCopied({text, label: 'Copied'}),
        () => setCopied({text, label: 'Could not copy'}),
      );
  };
  return (
    <>
      {ask === undefined ? null : (
        <Button variant="secondary" size={size} text="Ask about this error" press={ask} />
      )}
      {copy === undefined ? null : (
        <Button
          variant="secondary"
          size={size}
          text={copied?.text === copy ? copied.label : COPY}
          press={() => press(copy)}
        />
      )}
    </>
  );
}
