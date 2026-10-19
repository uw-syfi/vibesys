import type {JSX} from 'react';

/** Each icon's strokes on a 24 unit grid, drawn in the current text color. */
const PATHS = {
  plus: 'M12 5v14M5 12h14',
  chat: 'M4 5h16v11H9l-5 4z',
  'arrow-up': 'M12 19V5M5 12l7-7 7 7',
  'chevron-down': 'M6 9l6 6 6-6',
  'chevron-right': 'M9 6l6 6-6 6',
  'chevron-left': 'M15 6l-6 6 6 6',
  more: 'M4 12a1 1 0 1 0 2 0 1 1 0 1 0-2 0zM11 12a1 1 0 1 0 2 0 1 1 0 1 0-2 0zM18 12a1 1 0 1 0 2 0 1 1 0 1 0-2 0z',
  check: 'M5 12.5l4.5 4.5L19 7.5',
  cross: 'M6 6l12 12M18 6L6 18',
  clock: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v5l3 2',
  monitor: 'M5 4h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM8 20h8M12 16v4',
  gear: 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9L7 7M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1',
  folder: 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
  search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4',
  alert: 'M12 3l10 18H2zM12 10v4M12 16.5v2',
  info: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 11v6M12 6.5v2',
  external: 'M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5',
  refresh: 'M20 11a8 8 0 0 0-14.6-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.6 4.5L20 16M20 20v-4h-4',
  stop: 'M7 7h10v10H7z',
  play: 'M8 5l11 7-11 7z',
  circle: 'M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16z',
  grid: 'M4 5h6v6H4zM14 5h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z',
  usage: 'M5 20V11M12 20V5M19 20v-7',
  // The app's logo (the website's `logo.svg`): a terminal window with a prompt.
  mark: 'M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1zM7 9l3 3-3 3M13 15h4',
  sidebar: 'M5 4h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM9 4v16',
} as const;

/** The closed set of icons. */
export type IconName = keyof typeof PATHS;
export const ICON_NAMES = Object.keys(PATHS) as readonly IconName[];

/**
 * A stroke icon at the `icon` size. Decorative (hidden from assistive technology) unless it has a
 * `label`, which then names it.
 */
export function Icon(props: {readonly name: IconName; readonly label?: string}): JSX.Element {
  const named = props.label !== undefined;
  return (
    <svg
      className="vs-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      role={named ? 'img' : undefined}
      aria-label={props.label}
      aria-hidden={named ? undefined : true}
      focusable="false"
    >
      <path d={PATHS[props.name]} />
    </svg>
  );
}
