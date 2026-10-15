import {type JSX, useId, useSyncExternalStore} from 'react';
import {
  parseWebThemeName,
  selectedWebTheme,
  WEB_THEME_NAMES,
  type WebThemeController,
  type WebThemeName,
} from './theme.js';

export interface WebThemePickerProps {
  readonly theme: WebThemeController;
}

/** Application-shell appearance control; native controls provide browser keyboard semantics. */
export function WebThemePicker({theme}: WebThemePickerProps): JSX.Element {
  const state = useSyncExternalStore(theme.subscribe, theme.getState, theme.getState);
  const selectId = useId();
  const statusId = useId();
  const selected = selectedWebTheme(state);
  const overridden = state.override !== null;
  return (
    <section className="web-theme-picker" aria-label="Appearance preferences">
      <label htmlFor={selectId}>Theme</label>
      <select
        id={selectId}
        value={selected}
        aria-describedby={statusId}
        onChange={event => {
          const name = parseWebThemeName(event.currentTarget.value);
          if (name !== null) theme.dispatch({type: 'select', theme: name});
        }}
      >
        {WEB_THEME_NAMES.map(name => (
          <option key={name} value={name}>
            {themeLabel(name)}
          </option>
        ))}
      </select>
      <span id={statusId} className="web-theme-status" aria-live="polite">
        {overridden
          ? `Browser override. Host default: ${themeLabel(state.hostDefault)}.`
          : `Using host default: ${themeLabel(state.hostDefault)}.`}
      </span>
      <button type="button" disabled={!overridden} onClick={() => theme.dispatch({type: 'reset'})}>
        Use host default
      </button>
    </section>
  );
}

function themeLabel(name: WebThemeName): string {
  return name
    .split('-')
    .map(word => `${word.slice(0, 1).toUpperCase()}${word.slice(1)}`)
    .join(' ');
}
