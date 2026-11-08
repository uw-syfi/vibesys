import type {JSX, ReactNode} from 'react';
import {Text} from './text.js';

/**
 * A card that is one button: a title (with a small `tag` after it), an id line in the code face, and
 * small chips under them. `kicker` is a section-style label over the title (the kind of card).
 * `selected` is the card chosen among its grid's; `unavailable` says why it cannot be chosen, as a
 * small tag after the title, and keeps the card where it is.
 */
export function PickCard(props: {
  readonly title: string;
  readonly kicker?: string;
  readonly tag?: string;
  readonly id?: string;
  readonly note?: string;
  readonly chips?: ReactNode;
  readonly selected?: boolean;
  readonly unavailable?: string;
  readonly press: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      className="vs-pick-card"
      aria-pressed={props.selected === undefined ? undefined : props.selected}
      aria-disabled={props.unavailable === undefined ? undefined : true}
      data-unavailable={props.unavailable === undefined ? undefined : ''}
      onClick={() => {
        if (props.unavailable === undefined) props.press();
      }}
    >
      {props.kicker === undefined ? null : <Text variant="section">{props.kicker}</Text>}
      <span className="vs-pick-card-title">
        {props.title}
        {props.tag === undefined ? null : <span className="vs-pick-card-tag">{props.tag}</span>}
        {props.unavailable === undefined ? null : (
          <span className="vs-pick-card-tag">{props.unavailable}</span>
        )}
      </span>
      {props.id === undefined ? null : <span className="vs-pick-card-id">{props.id}</span>}
      {props.note === undefined ? null : <span className="vs-pick-card-note">{props.note}</span>}
      {props.chips === undefined ? null : <span className="vs-pick-card-chips">{props.chips}</span>}
    </button>
  );
}

/**
 * Cards in a grid of equal columns, none narrower than the field column: `fill` keeps empty
 * columns (a catalogue's cards keep their size whatever their number), `fit` stretches the cards
 * to the row (two entry cards).
 */
export function PickCards(props: {
  readonly label: string;
  readonly columns: 'fill' | 'fit';
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <fieldset className="vs-pick-cards" aria-label={props.label} data-columns={props.columns}>
      {props.children}
    </fieldset>
  );
}
