import type {JSX} from 'react';

/**
 * One cell of a lineage guide: a line through (`pipe`), a branch (`tee`), the last branch
 * (`elbow`), a dotted line for levels left out (`gap`), or none.
 */
export type Guide = 'pipe' | 'tee' | 'elbow' | 'gap' | 'blank';

/**
 * The lineage cells before a name, one per ancestor level. A row on the path the reader follows
 * to the root draws them in accent (its row's `data-path`, in CSS).
 */
export function Guides(props: {readonly guides: readonly Guide[] | undefined}): JSX.Element {
  const cells = props.guides ?? [];
  return (
    <>
      {cells.map((guide, level) => (
        <span
          // biome-ignore lint/suspicious/noArrayIndexKey: a cell is its level from the root; the cells of a row never reorder and carry no state.
          key={level}
          className="vs-guide"
          data-guide={guide}
          aria-hidden="true"
        />
      ))}
    </>
  );
}
