/**
 * One idea as a row of the Ideas list, flat or as a node of the lineage tree. The tree adds its
 * place (guides, depth), whether the row folds what is below it, and the pointer's hooks; a flat
 * row adds the `from` column instead.
 */
import {type Guide, markdownToPlain, PickRow} from '@vibesys/ui';
import type {JSX} from 'react';
import {ageWords, type IdeaRow, type ImplementerOutcomes} from '../../state/index.js';
import type {Zoom} from './views/props.js';

/** What a row of the tree knows beyond a flat row. */
export interface TreePlace {
  readonly guides: readonly Guide[];
  /** Steps from the input, when the guides stop short of it. */
  readonly depth: number | null;
  /** The row's own summary line: what is below it, while it is folded. */
  readonly summary: string | null;
  readonly fold: {readonly open: boolean; readonly toggle: () => void} | 'none';
  /** On the ancestry of the idea the reader points at or pinned. */
  readonly lit: boolean;
  readonly hover: (on: boolean) => void;
}

/** A row's figure: its age while it runs, else its figure, with the whole on hover when it is short. */
function rowFigure(
  row: IdeaRow,
  now: number | null,
): {readonly figure: string; readonly figureTitle?: string} {
  if (row.runningSince !== null) return {figure: ageWords(row.runningSince, now)};
  return row.figureTitle === undefined
    ? {figure: row.figure}
    : {figure: row.figure, figureTitle: row.figureTitle};
}

export function IdeaPickRow(props: {
  readonly row: IdeaRow;
  readonly outcomes: ImplementerOutcomes;
  readonly compact: boolean;
  readonly now: number | null;
  readonly zoom: Zoom;
  readonly place?: TreePlace;
}): JSX.Element {
  const {row, compact, zoom, place} = props;
  const reported = props.outcomes.get(row.id)?.outcome ?? null;
  // The idea's own text is Markdown; a row shows it as one line of plain words.
  const summary = place?.summary ?? (row.summary === null ? null : markdownToPlain(row.summary));
  return (
    <PickRow
      id={row.id}
      title={markdownToPlain(row.title)}
      mono
      {...(compact || summary === null ? {} : {summary})}
      state={row.state}
      badges={compact || reported === null ? row.badges : [...row.badges, reported]}
      {...(compact ? {} : {bar: row.bar})}
      {...rowFigure(row, props.now)}
      figureTone={row.figureTone}
      {...(compact || place !== undefined
        ? {}
        : {trail: {label: markdownToPlain(row.trail.label)}})}
      {...(place === undefined
        ? {}
        : {
            guides: place.guides,
            fold: place.fold,
            path: place.lit,
            hover: place.hover,
            ...(place.depth === null
              ? {}
              : {lead: String(place.depth), leadTitle: `${place.depth} steps from input`}),
          })}
      stripe={row.stripe}
      fresh={row.fresh}
      select={() => zoom({type: 'open-idea', idea: row.id})}
    />
  );
}
