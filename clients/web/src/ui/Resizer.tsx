import {type KeyboardEvent, useState} from 'react';

export interface ResizerProps {
  label: string;
  edge: 'left' | 'right';
  value: number;
  min: number;
  max: number;
  /** +1 when ArrowRight widens the panel (sidebar), -1 when it narrows it (right pane). */
  grow: 1 | -1;
  widthAt: (clientX: number) => number;
  onChange: (width: number) => void;
}

const STEP = 16;

/** A window splitter: drag it, or focus it and use the arrow keys. */
export function Resizer({label, edge, value, min, max, grow, widthAt, onChange}: ResizerProps) {
  const [dragging, setDragging] = useState(false);
  const onKeyDown = (event: KeyboardEvent<HTMLHRElement>) => {
    const delta = event.key === 'ArrowRight' ? STEP : event.key === 'ArrowLeft' ? -STEP : 0;
    if (delta === 0) return;
    event.preventDefault();
    onChange(value + delta * grow);
  };
  return (
    <hr
      tabIndex={0}
      aria-label={label}
      aria-orientation="vertical"
      aria-valuenow={value}
      aria-valuemin={min}
      aria-valuemax={max}
      className={`drag ${edge}${dragging ? ' on' : ''}`}
      title="Drag to resize"
      onPointerDown={event => {
        event.currentTarget.setPointerCapture(event.pointerId);
        setDragging(true);
      }}
      onPointerMove={event => {
        if (dragging) onChange(widthAt(event.clientX));
      }}
      onPointerUp={() => setDragging(false)}
      onKeyDown={onKeyDown}
    />
  );
}
