import {describe, expect, it} from 'bun:test';
import {type CliRenderer, KeyEvent, type ScrollBoxRenderable} from '@opentui/core';
import type {SessionController} from '../session-controller.js';
import {initialSessionState, type SessionState} from '../session-model.js';
import type {SelectionClipboard} from './clipboard.js';
import {bindKeybindings, type KeybindingActions} from './keybindings.js';

/**
 * The router under test is one plain `keypress` handler, so it is driven
 * directly: a fake key input captures the handler `bindKeybindings` registers
 * and feeds it constructed `KeyEvent`s, and a recording controller answers
 * `state` with whatever session the test poses. No rendered surface is
 * involved; what is pinned here is which handler claims a key, not what the
 * screen looks like afterwards.
 */
class FakeKeyInput {
  readonly #handlers = new Set<(key: KeyEvent) => void>();

  on(_event: 'keypress', handler: (key: KeyEvent) => void): void {
    this.#handlers.add(handler);
  }

  off(_event: 'keypress', handler: (key: KeyEvent) => void): void {
    this.#handlers.delete(handler);
  }

  emit(key: KeyEvent): void {
    for (const handler of this.#handlers) handler(key);
  }
}

function fakeRenderer(keyInput: FakeKeyInput): CliRenderer {
  return {keyInput, terminalWidth: 120, destroy: () => {}} as unknown as CliRenderer;
}

/** Answers `state` and records every method the router invokes, as a no-op. */
function recordingController(state: SessionState): {
  controller: SessionController;
  calls: string[];
} {
  const calls: string[] = [];
  const controller = new Proxy(
    {},
    {
      get(_target, property) {
        if (property === 'state') return state;
        return (): void => {
          calls.push(String(property));
        };
      },
    },
  ) as SessionController;
  return {controller, calls};
}

function stubViewport(): ScrollBoxRenderable {
  return {
    scrollTo: () => {},
    scrollBy: () => {},
    scrollHeight: 0,
  } as unknown as ScrollBoxRenderable;
}

function stubClipboard(): SelectionClipboard {
  return {copySelection: () => 'no-selection'} as unknown as SelectionClipboard;
}

function stubActions(calls: string[] = []): KeybindingActions {
  return {
    completeInput: () => false,
    navigateSuggestions: () => false,
    navigateChatSuggestions: () => false,
    completeChatInput: () => false,
    inputIsEmpty: () => true,
    closeChat: () => calls.push('closeChat'),
    toggleLatestPrompt: () => {},
    toggleSelectedTool: () => false,
    revealSelectedEntry: () => {},
    revealOlderEntries: () => {},
    selectNextAgent: () => {},
    selectPreviousAgent: () => {},
    selectNextRound: () => {},
    selectPreviousRound: () => {},
    toggleTodos: () => {},
    setGraphWidthOverride: () => {},
    setChatWidthOverride: () => {},
    graphWidthOverride: () => null,
    chatWidthOverride: () => null,
    scrollRightPane: () => {},
    scrollChatPane: () => {},
    scrollExperimentDetail: () => {},
    scrollErrorBanner: () => {},
    scrollOverlay: () => {},
    clearTransientStatus: () => {},
    showClipboardStatus: () => {},
    runPaletteSelection: () => {},
    promoteNotepadToSteer: () => {},
    promoteNotepadToChat: () => {},
  };
}

function pressKey(name: string): KeyEvent {
  return new KeyEvent({
    name,
    ctrl: false,
    meta: false,
    shift: false,
    option: false,
    sequence: '',
    number: false,
    raw: '',
    eventType: 'press',
    source: 'raw',
  });
}

function pressCtrlKey(name: string): KeyEvent {
  return new KeyEvent({
    name,
    ctrl: true,
    meta: false,
    shift: false,
    option: false,
    sequence: '',
    number: false,
    raw: '',
    eventType: 'press',
    source: 'raw',
  });
}

function bound(state: SessionState): {keyInput: FakeKeyInput; calls: string[]} {
  const keyInput = new FakeKeyInput();
  const {controller, calls} = recordingController(state);
  bindKeybindings(
    fakeRenderer(keyInput),
    controller,
    stubViewport(),
    stubClipboard(),
    stubActions(calls),
  );
  return {keyInput, calls};
}

describe('F4 pane zoom', () => {
  it('toggles the pane zoom while no modal is open', () => {
    const {keyInput, calls} = bound(initialSessionState());

    const key = pressKey('f4');
    keyInput.emit(key);

    expect(calls).toEqual(['togglePaneZoom']);
    expect(key.defaultPrevented).toBe(true);
  });

  it('is inert while the palette is open', () => {
    const {keyInput, calls} = bound({
      ...initialSessionState(),
      palette: {query: '', selected: 0},
    });

    const key = pressKey('f4');
    keyInput.emit(key);

    expect(calls).not.toContain('togglePaneZoom');
    // The palette is modal: the key is swallowed rather than left to fall
    // through to the panes, the same claim every other modal already has.
    expect(key.defaultPrevented).toBe(true);
  });
});

describe('Ctrl+L live routing', () => {
  it('returns from a hypothesis summary to the experiment log', () => {
    const {keyInput, calls} = bound({
      ...initialSessionState(),
      hypothesisDetail: {entryKey: 'H-01', selectedRound: 1},
    });

    const key = pressCtrlKey('l');
    keyInput.emit(key);

    expect(calls).toEqual(['live']);
    expect(key.defaultPrevented).toBe(true);
  });

  it('returns to the experiment log while the docked chat has focus', () => {
    const {keyInput, calls} = bound({
      ...initialSessionState(),
      layout: {right: null, focus: 'chat', zoomedPane: null},
    });

    const key = pressCtrlKey('l');
    keyInput.emit(key);

    expect(calls).toEqual(['live']);
    expect(key.defaultPrevented).toBe(true);
  });

  it('leaves Ctrl+L with a modal chat rather than closing the foreground modal', () => {
    const {keyInput, calls} = bound({...initialSessionState(), chatOpen: true});

    const key = pressCtrlKey('l');
    keyInput.emit(key);

    expect(calls).toEqual([]);
    expect(key.defaultPrevented).toBe(false);
  });
});

describe('foreground modal key ownership', () => {
  const initial = initialSessionState();
  const banner: NonNullable<SessionState['errorBanner']> = {
    titleKind: 'run',
    message: 'Run failed',
    detail: null,
    hint: null,
    diagnosticId: 'diagnostic-1',
    severity: 'fatal',
    scope: 'run',
    agentKind: null,
    roundLabel: null,
    invocationId: null,
    count: 1,
  };
  const modals: Array<{
    name: string;
    state: Partial<SessionState>;
    closesWith: string;
  }> = [
    {
      name: 'notepad',
      state: {notepad: {...initial.notepad, open: true}},
      closesWith: 'closeNotepad',
    },
    {
      name: 'command palette',
      state: {palette: {query: '', selected: 0}},
      closesWith: 'closePalette',
    },
    {
      name: 'chat menu',
      state: {
        chatMenu: {
          kind: 'model',
          title: 'Harness and model',
          rows: [{kind: 'note', label: 'Loading options…'}],
          selected: -1,
          pending: true,
          error: null,
          customModels: {},
        },
      },
      closesWith: 'closeChatMenu',
    },
    {
      name: 'theme picker',
      state: {themePicker: {selected: 'dark'}},
      closesWith: 'closeThemePicker',
    },
    {
      name: 'diff viewer',
      state: {
        diffViewer: {
          round: 1,
          base: 'base',
          head: 'head',
          files: [{path: 'src/main.ts', change: 'modified'}],
          index: 0,
          hunk: 0,
          patches: {},
        },
      },
      closesWith: 'closeDiffViewer',
    },
    {
      name: 'command overlay',
      state: {overlay: {kind: 'detail', content: 'Command result'}},
      closesWith: 'live',
    },
    {
      name: 'modal chat',
      state: {chatOpen: true},
      closesWith: 'closeChat',
    },
  ];

  for (const error of [{errorBanner: banner}, {inputError: 'Invalid command'}]) {
    const errorName = 'errorBanner' in error ? 'error banner' : 'input error';
    for (const modal of modals) {
      it(`keeps Escape on the ${modal.name} ahead of the ${errorName}`, () => {
        const {keyInput, calls} = bound({...initialSessionState(), ...error, ...modal.state});

        const key = pressKey('escape');
        keyInput.emit(key);

        expect(calls).toEqual([modal.closesWith]);
        expect(key.defaultPrevented).toBe(true);
      });
    }
  }

  for (const errorCase of [
    {
      name: 'error banner',
      state: {errorBanner: banner},
      clearsWith: 'dismissErrorBanner',
    },
    {
      name: 'input error',
      state: {inputError: 'Invalid command'},
      clearsWith: 'clearInputError',
    },
  ]) {
    it(`dismisses the ${errorCase.name} once no modal is open`, () => {
      const {keyInput, calls} = bound({...initialSessionState(), ...errorCase.state});

      const key = pressKey('escape');
      keyInput.emit(key);

      expect(calls).toEqual([errorCase.clearsWith]);
      expect(key.defaultPrevented).toBe(true);
    });
  }
});
