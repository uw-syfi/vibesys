import {type JSX, type KeyboardEvent, type ReactNode, useId, useRef} from 'react';
import {rovingStep} from './roving.js';
import type {ChoiceOption} from './segmented-control.js';

/**
 * A tab list over the panel `children` renders for `value`. One tab stop (the chosen tab); the
 * arrows, Home and End move the focus and choose (automatic activation).
 */
export function Tabs<T extends string>(props: {
  readonly label: string;
  readonly tabs: readonly ChoiceOption<T>[];
  readonly value: T;
  readonly change: (value: T) => void;
  readonly children: ReactNode;
}): JSX.Element {
  const {tabs, value, change} = props;
  const id = useId();
  const list = useRef<HTMLDivElement>(null);
  const tabId = (tab: T): string => `${id}-tab-${tab}`;
  const keyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number): void => {
    const next = rovingStep(event.key, index, tabs.length);
    const tab = next === null ? undefined : tabs[next];
    if (next === null || tab === undefined) return;
    event.preventDefault();
    list.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
    if (tab.value !== value) change(tab.value);
  };
  return (
    <div className="vs-tabs">
      <div ref={list} className="vs-tab-list" role="tablist" aria-label={props.label}>
        {tabs.map((tab, index) => (
          <button
            key={tab.value}
            id={tabId(tab.value)}
            type="button"
            role="tab"
            className="vs-tab"
            aria-selected={tab.value === value}
            aria-controls={`${id}-panel`}
            tabIndex={tab.value === value ? 0 : -1}
            onClick={() => {
              if (tab.value !== value) change(tab.value);
            }}
            onKeyDown={event => keyDown(event, index)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div id={`${id}-panel`} role="tabpanel" aria-labelledby={tabId(value)}>
        {props.children}
      </div>
    </div>
  );
}
