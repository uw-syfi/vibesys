import type {ProsePart} from '../model.js';

/** Paragraphs from `prose()`: text, inline code, and bold. The caller styles the wrapper. */
export function Prose({paragraphs}: {paragraphs: ProsePart[][]}) {
  return paragraphs.map((parts, paragraph) => (
    // biome-ignore lint/suspicious/noArrayIndexKey: paragraphs of one text never reorder.
    <p key={paragraph}>
      {parts.map((part, index) =>
        part.kind === 'code' ? (
          // biome-ignore lint/suspicious/noArrayIndexKey: parts of one paragraph never reorder.
          <code key={index}>{part.text}</code>
        ) : part.kind === 'strong' ? (
          // biome-ignore lint/suspicious/noArrayIndexKey: parts of one paragraph never reorder.
          <strong key={index}>{part.text}</strong>
        ) : (
          part.text
        ),
      )}
    </p>
  ));
}
