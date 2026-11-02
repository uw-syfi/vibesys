import type {JSX} from 'react';
import {Markdown} from './markdown.js';
import {Text} from './text.js';

/**
 * One message of a chat, with no author label or frame. On the `end` side it is a compact bubble at
 * the end of the line (plain text, line breaks kept); on the `start` side it is written text
 * (Markdown) on the surface. `notice` is a short word under it, such as that it is still being
 * written; muted, or in the error tone with the text when `tone` is `err`.
 */
export function ChatMessage(props: {
  readonly side: 'start' | 'end';
  readonly text: string;
  readonly notice?: string;
  readonly tone?: 'default' | 'err';
}): JSX.Element {
  const tone = props.tone ?? 'default';
  return (
    <div className="vs-chat-message" data-side={props.side} data-tone={tone}>
      <div className="vs-chat-message-text" data-markdown={props.side === 'start'}>
        {props.side === 'start' ? <Markdown text={props.text} /> : props.text}
      </div>
      {props.notice === undefined ? null : (
        <Text variant="small" tone={tone === 'err' ? 'err' : 'muted'}>
          {props.notice}
        </Text>
      )}
    </div>
  );
}
