/**
 * Filling a form as a person does, for DOM tests: a field is found by its label, typed into with the
 * native setter and the events a browser sends, and a select is chosen by value.
 */
import {act} from 'react';
import type {Screen} from './harness.js';

/** The control a field's label names, found as a person finds it: by the label. */
export function fieldOf<T extends HTMLElement>(screen: Screen, label: string): T {
  const named = [...screen.container.querySelectorAll('label')].find(
    each => each.textContent?.trim() === label,
  );
  const control =
    named === undefined ? null : screen.container.querySelector<T>(`#${named.htmlFor}`);
  if (control === null) throw new Error(`no field named ${label}`);
  return control;
}

/** Types `text` into a text field: focus, the native value setter, then input, change and key release. */
export function typeInto(field: HTMLInputElement | HTMLTextAreaElement, text: string): void {
  const prototype = Object.getPrototypeOf(field) as object;
  const set = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
  if (set === undefined) throw new Error('a text field has a value setter');
  const View = field.ownerDocument.defaultView as unknown as {Event: typeof Event};
  act(() => {
    field.focus();
    set.call(field, text);
    for (const name of ['input', 'change', 'keyup'])
      field.dispatchEvent(new View.Event(name, {bubbles: true}));
  });
}

/** Chooses the option with `value` in a select. */
export function choose(select: HTMLSelectElement, value: string): void {
  const View = select.ownerDocument.defaultView as unknown as {Event: typeof Event};
  act(() => {
    select.value = value;
    select.dispatchEvent(new View.Event('change', {bubbles: true}));
  });
}
