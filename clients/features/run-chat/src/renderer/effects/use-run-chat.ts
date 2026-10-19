/**
 * The run chat's view over one run: the panel machine and the steer cards, created when the screen
 * shows a run and stopped when it shows another, read as the panel and the Chat button's state.
 * The transcript, the threads and the proposals come from the run fold (`core`), the sends and
 * the catalogue from the injected ports.
 */
import type {HarnessChoice} from '@vibesys/ipc';
import {actorStore} from '@vibesys/machine';
import {type CoreState, initialCoreState} from '@vibesys/run-state';
import {useEffect, useRef, useState, useSyncExternalStore} from 'react';
import {type Actor, createActor, fromPromise} from 'xstate';
import {
  type CardItem,
  type CardValue,
  type ChatPanelView,
  cardView,
  deliveriesOf,
  type IdeaOf,
  type PanelContext,
  type PanelItem,
  panelMachine,
  runHarness,
  runRoles,
  shownThread,
  threadEntries,
  threadItems,
} from '../../state/index.js';
import {type CardSnapshot, SteerCards} from './cards.js';
import type {RunChatPorts} from './ports.js';

export interface RunChatOptions {
  /** One run's identity: a new one starts a new chat. */
  readonly identity: string;
  /** The run fold; null before the run reached the window. */
  readonly core: CoreState | null;
  readonly ports: RunChatPorts;
  /** The number of the idea an attempt belongs to, for "reached idea N". */
  readonly ideaOf?: IdeaOf;
  /** Roles seen on the run's agents, besides those its start advertised: the steer targets. */
  readonly roles?: readonly string[];
  /** The run cannot take a steer (it ended): its cards keep their drafts and cannot send. */
  readonly readOnly?: boolean;
}

/** What the run screen hosts: whether the panel is open, the button's press, and the panel. */
export interface RunChatHost {
  readonly open: boolean;
  readonly toggle: () => void;
  /** What the panel draws; null while it is closed. */
  readonly panel: ChatPanelView | null;
}

type PanelActor = Actor<typeof panelMachine>;
type PanelSnapshot = ReturnType<PanelActor['getSnapshot']>;

interface Chat {
  readonly actor: PanelActor;
  readonly cards: SteerCards;
  readonly store: ReturnType<typeof actorStore<typeof panelMachine, PanelSnapshot>>;
}

const NO_CORE = initialCoreState();

function createChat(portsRef: {readonly current: RunChatPorts}): Chat {
  const cards = new SteerCards({
    control: request => portsRef.current.control(request),
    newControlId: () => portsRef.current.newControlId(),
  });
  const machine = panelMachine.provide({
    actors: {
      loadCatalogue: fromPromise(() => portsRef.current.harnesses()),
      createThread: fromPromise(({input, signal}) =>
        portsRef.current.createThread(input.harness, signal),
      ),
      ask: fromPromise(({input, signal}) => portsRef.current.ask(input, signal)),
    },
  });
  const actor = createActor(machine);
  actor.start();
  return {actor, cards, store: actorStore(actor, snapshot => snapshot)};
}

function cardItem(chat: Chat, id: string, snapshot: CardSnapshot): CardItem {
  const {cards} = chat;
  const {context} = snapshot;
  return {
    id,
    draft: context.draft,
    view: cardView(context, snapshot.value as CardValue),
    actions: {
      editText: text => cards.send(id, {type: 'edit-text', text}),
      setTarget: target => cards.send(id, {type: 'set-target', target}),
      setMode: mode => cards.send(id, {type: 'set-mode', mode}),
      send: () => cards.sendSteer(id),
      dismiss: () => cards.send(id, {type: 'dismiss'}),
      withdraw: () => cards.withdraw(id),
    },
  };
}

/** The panel's items for the thread on screen, with a live card for each steer. */
function panelItems(
  chat: Chat,
  core: CoreState,
  shown: string | null,
  panel: PanelContext,
  readOnly: boolean,
) {
  const pending =
    panel.pending !== null && panel.pending.threadId === shown ? panel.pending.text : null;
  const items: PanelItem[] = [];
  const addCard = (id: string, draft: CardItem['draft']): void => {
    chat.cards.ensure({
      id,
      origin: id.startsWith('user ') ? 'user' : 'proposal',
      draft,
      readOnly,
    });
    const snapshot = chat.cards.snapshot(id);
    if (snapshot !== null) items.push({kind: 'card', card: cardItem(chat, id, snapshot)});
  };
  for (const item of shown === null ? [] : threadItems(core, shown, pending)) {
    if (item.kind !== 'proposal') {
      items.push(item);
      continue;
    }
    const {proposal} = item;
    addCard(proposal.id, {
      text: proposal.text,
      target: {kind: proposal.target.kind, id: proposal.target.id},
      mode: proposal.mode,
    });
  }
  if (shown === null && pending !== null) {
    items.push({kind: 'question', id: 'pending', text: pending});
    items.push({kind: 'waiting', id: 'waiting'});
  }
  for (const steer of panel.userSteers) addCard(steer.id, steer.draft);
  return items;
}

/** The pairs a new chat starts on, best first: the last thread's own, then the run's. */
function preferredHarnesses(core: CoreState): readonly (HarnessChoice | null)[] {
  const last = core.chatThreads.find(thread => thread.id === core.lastChatThreadId);
  const lastPair =
    last?.provider != null && last.model != null
      ? {provider: last.provider, model: last.model}
      : null;
  return [lastPair, runHarness(core)];
}

export function useRunChat(options: RunChatOptions): RunChatHost {
  const {identity, ports} = options;
  const core = options.core ?? NO_CORE;
  const portsRef = useRef(ports);
  portsRef.current = ports;
  const [chat, setChat] = useState<Chat | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: the chat belongs to one run (`identity`); the ports are read through `portsRef`, so a new but equal ports object must not dispose it.
  useEffect(() => {
    const created = createChat(portsRef);
    setChat(created);
    return () => {
      created.actor.stop();
      created.cards.stop();
      setChat(null);
    };
  }, [identity]);
  const none = useRef({getState: () => null, subscribe: () => () => {}}).current;
  const source = chat?.store ?? none;
  const panel = useSyncExternalStore(source.subscribe, source.getState, source.getState);
  const cards = chat?.cards;
  useSyncExternalStore(
    cards?.subscribe ?? none.subscribe,
    cards?.version ?? (() => 0),
    cards?.version ?? (() => 0),
  );
  const preferred = preferredHarnesses(core);
  const preferredKey = JSON.stringify(preferred);
  // biome-ignore lint/correctness/useExhaustiveDependencies: `preferred` is rebuilt every render; its serialized form says when it changed.
  useEffect(() => {
    chat?.actor.send({type: 'set-preferred', preferred});
  }, [chat, preferredKey]);
  const readOnly = options.readOnly === true;
  useEffect(() => {
    cards?.setReadOnly(readOnly);
  }, [cards, readOnly]);
  const ideaOf = options.ideaOf;
  const deliveries = core.steerDeliveries;
  // biome-ignore lint/correctness/useExhaustiveDependencies: deliveries are re-read from the fold whenever it holds new steps; `core` itself changes with every event.
  useEffect(() => {
    if (cards !== undefined) cards.deliver(deliveriesOf(core, ideaOf));
  }, [cards, deliveries, ideaOf]);
  if (chat === null || panel === null) return {open: false, toggle: () => undefined, panel: null};
  const {context, value} = panel;
  const open = typeof value === 'object' && value.visibility === 'open';
  const chatState = typeof value === 'object' ? value.chat : 'idle';
  const shown = shownThread(core, context.choice);
  return {
    open,
    toggle: () => chat.actor.send({type: 'toggle'}),
    panel: open
      ? {
          panel: context,
          busy: chatState !== 'idle',
          threads: threadEntries(core),
          shown,
          items: panelItems(chat, core, shown, context, readOnly),
          roles: runRoles(core.expectedRoles, options.roles ?? []),
          dispatch: event => chat.actor.send(event),
        }
      : null,
  };
}
