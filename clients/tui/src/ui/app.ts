import {BoxRenderable, type CliRenderer, ScrollBoxRenderable, TextRenderable} from '@opentui/core';
import type {SessionController} from '../session-controller.js';
import {type SessionState, statusText} from '../session-model.js';
import {ConversationView} from './conversation.js';
import {AgentMapView} from './agent-map.js';
import {createInputPanel} from './input.js';
import {bindKeybindings} from './keybindings.js';
import {RoundStripView} from './round-strip.js';
import {OverlayView} from './overlay.js';
import {createMarkdownStyle} from './styles.js';

export interface OpenTuiApp {
  destroy(): void;
}

export function createOpenTuiApp(renderer: CliRenderer, controller: SessionController): OpenTuiApp {
  const root = new BoxRenderable(renderer, {
    id: 'app',
    width: '100%',
    height: '100%',
    flexDirection: 'column',
  });
  const header = new TextRenderable(renderer, {
    id: 'header',
    height: 1,
    fg: '#22d3ee',
    content: 'VibeSys · connecting',
  });
  const viewport = new ScrollBoxRenderable(renderer, {
    id: 'viewport',
    width: 'auto',
    flexGrow: 1,
    border: true,
    borderStyle: 'rounded',
    borderColor: '#475569',
    stickyScroll: true,
    stickyStart: 'bottom',
    viewportCulling: true,
    verticalScrollbarOptions: {showArrows: true},
  });
  const main = new BoxRenderable(renderer, {
    id: 'main',
    width: '100%',
    flexGrow: 1,
    flexDirection: 'row',
  });
  const help = new TextRenderable(renderer, {
    id: 'key-help',
    height: 1,
    fg: '#64748b',
    content: '[/]: round · Tab: agent · PgUp/PgDn · Ctrl+P: prompt · Ctrl+L: live',
  });
  const markdownStyle = createMarkdownStyle();
  const roundStrip = new RoundStripView(renderer, controller);
  const agentMap = new AgentMapView(renderer);
  const overlay = new OverlayView(renderer);
  const conversation = new ConversationView(renderer, controller, markdownStyle);
  const input = createInputPanel(renderer, value => void controller.submit(value));

  viewport.add(conversation.output);
  main.add(agentMap.output);
  main.add(viewport);
  root.add(header);
  root.add(roundStrip.output);
  root.add(main);
  root.add(help);
  root.add(input.box);
  root.add(overlay.output);
  renderer.root.add(root);
  input.focus();

  const render = (state: SessionState): void => {
    const returnHint = state.overlay === null ? '' : ' · Esc: close dialog';
    const selection = state.selectedAgentKind ? ` · selected ${state.selectedAgentKind}` : '';
    header.content = `VibeSys · ${statusText(state)}${selection}${returnHint}`;
    roundStrip.render(state);
    agentMap.render(state);
    conversation.render(state);
    overlay.render(state);
  };
  const unbindKeys = bindKeybindings(renderer, controller, viewport, {
    toggleLatestPrompt: () => conversation.toggleLatestPrompt(),
    selectNextAgent: () => controller.selectNextAgent(),
    selectPreviousAgent: () => controller.selectPreviousAgent(),
    selectNextRound: () => controller.selectNextRound(),
    selectPreviousRound: () => controller.selectPreviousRound(),
  });
  const unsubscribe = controller.subscribe(render);

  return {
    destroy(): void {
      unsubscribe();
      unbindKeys();
      input.destroy();
      root.destroyRecursively();
      markdownStyle.destroy();
    },
  };
}
