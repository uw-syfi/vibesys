import {
  BoxRenderable,
  type CliRenderer,
  InputRenderable,
  InputRenderableEvents,
  SyntaxStyle,
  TextRenderable,
} from '@opentui/core';
import {type SlashCommand, slashCommandRange, suggestSlashCommands} from '../commands.js';

export interface InputPanel {
  box: BoxRenderable;
  suggestions: BoxRenderable;
  completeSuggestion(): boolean;
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
  const syntaxStyle = SyntaxStyle.fromStyles({
    'slash-command': {fg: '#22d3ee', bold: true},
  });
  const commandStyleId = syntaxStyle.getStyleId('slash-command');
  const input = new InputRenderable(renderer, {
    id: 'input',
    width: '100%',
    placeholder: 'Type a question or /help',
    textColor: '#f8fafc',
    focusedTextColor: '#f8fafc',
    syntaxStyle,
  });
  const suggestions = new BoxRenderable(renderer, {
    id: 'input-suggestions',
    position: 'absolute',
    bottom: 3,
    left: 0,
    width: '100%',
    height: 3,
    visible: false,
    zIndex: 5,
    border: true,
    borderStyle: 'rounded',
    borderColor: '#475569',
    backgroundColor: '#0f172a',
    paddingLeft: 1,
    paddingRight: 1,
  });
  const suggestionList = new TextRenderable(renderer, {
    id: 'input-suggestion-list',
    width: '100%',
    height: 1,
    fg: '#94a3b8',
    wrapMode: 'none',
    truncate: true,
    content: '',
  });
  suggestions.add(suggestionList);
  let matches: readonly SlashCommand[] = [];

  const updateDecorations = (value: string): void => {
    input.clearAllHighlights();
    const range = slashCommandRange(value);
    if (range !== null && commandStyleId !== null) {
      input.addHighlightByCharRange({...range, styleId: commandStyleId});
    }

    matches = suggestSlashCommands(value);
    const visible = matches.length > 0;
    suggestions.visible = visible;
    suggestions.height = matches.length + 2;
    suggestionList.height = Math.max(1, matches.length);
    suggestionList.content = matches
      .map(
        (command, index) =>
          `${index === 0 ? '›' : ' '} ${command.name.padEnd(10)} ${command.description}${
            index === 0 && command.name !== value ? '  [Tab]' : ''
          }`,
      )
      .join('\n');
  };
  const submit = (value: string): void => {
    input.value = '';
    onSubmit(value);
  };
  input.on(InputRenderableEvents.INPUT, updateDecorations);
  input.on(InputRenderableEvents.ENTER, submit);
  box.add(input);
  return {
    box,
    suggestions,
    completeSuggestion(): boolean {
      const suggestion = matches[0];
      if (suggestion === undefined || suggestion.name === input.value) return false;
      input.value = suggestion.name;
      return true;
    },
    focus: () => input.focus(),
    destroy(): void {
      input.off(InputRenderableEvents.INPUT, updateDecorations);
      input.off(InputRenderableEvents.ENTER, submit);
      if (!input.isDestroyed) input.syntaxStyle = null;
      syntaxStyle.destroy();
    },
  };
}
