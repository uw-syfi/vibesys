import {
  BoxRenderable,
  type CliRenderer,
  InputRenderable,
  InputRenderableEvents,
} from '@opentui/core';

export interface InputPanel {
  box: BoxRenderable;
  focus(): void;
  destroy(): void;
}

export function createInputPanel(
  renderer: CliRenderer,
  onSubmit: (value: string) => void,
): InputPanel {
  const box = new BoxRenderable(renderer, {
    id: 'input-box',
    height: 3,
    width: '100%',
    border: true,
    borderStyle: 'rounded',
    borderColor: '#22c55e',
    title: ' Ask or command ',
    paddingLeft: 1,
    paddingRight: 1,
  });
  const input = new InputRenderable(renderer, {
    id: 'input',
    width: '100%',
    placeholder: 'Type a question or /help',
    textColor: '#f8fafc',
    focusedTextColor: '#f8fafc',
  });
  const submit = (value: string): void => {
    input.value = '';
    onSubmit(value);
  };
  input.on(InputRenderableEvents.ENTER, submit);
  box.add(input);
  return {
    box,
    focus: () => input.focus(),
    destroy: () => input.off(InputRenderableEvents.ENTER, submit),
  };
}
