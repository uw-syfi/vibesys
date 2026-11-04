import type {CSSProperties} from 'react';

/** An inline style that may also set custom properties, which `components.css` reads. */
export type StyleWithVars = CSSProperties & Readonly<Record<`--${string}`, string | number>>;
