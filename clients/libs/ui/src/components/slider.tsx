import type {JSX} from 'react';
import {useFieldWiring} from './field.js';

/** A discrete native range control inside a Field, with names for each stop. */
export function Slider(props: {
  readonly options: readonly string[];
  readonly value: number;
  readonly change: (index: number) => void;
}): JSX.Element {
  const wiring = useFieldWiring('Slider');
  return (
    <div className="vs-slider">
      <input
        id={wiring.id}
        type="range"
        min={0}
        max={props.options.length - 1}
        step={1}
        value={props.value}
        aria-valuetext={props.options[props.value]}
        aria-describedby={wiring.describedBy}
        onChange={event => props.change(Number(event.target.value))}
      />
      <div className="vs-slider-labels" aria-hidden="true">
        {props.options.map(option => (
          <span key={option}>{option}</span>
        ))}
      </div>
    </div>
  );
}
