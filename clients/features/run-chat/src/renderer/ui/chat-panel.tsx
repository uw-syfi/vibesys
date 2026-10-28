import {
  Button,
  ChatMessage,
  Field,
  Menu,
  MenuBounds,
  Row,
  Select,
  Stack,
  Text,
  TextInput,
  Thread,
  type ThreadItem as UiThreadItem,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type ChatPanelView,
  effectiveHarness,
  modelOptions,
  type PanelItem,
  providerOptions,
  withProvider,
} from '../../state/index.js';
import {ChatBoundary} from './chat-boundary.js';
import {FailureNote} from './failure-note.js';
import {SteerCardView} from './steer-card-view.js';

/** The harness, then the model: the two choices of a new chat in one compact row. */
function Picker(props: Pick<ChatPanelView, 'panel' | 'dispatch'>): JSX.Element | null {
  const {panel, dispatch} = props;
  const {catalogue} = panel;
  const harness = effectiveHarness(panel);
  if (catalogue.kind === 'loading') {
    return (
      <Text variant="small" tone="muted">
        Loading
      </Text>
    );
  }
  if (catalogue.kind === 'none' || harness === null) return null;
  const {offers} = catalogue;
  return (
    <Row gap="space-3" wrap>
      <Field label="Harness" hideLabel>
        <Select
          size="sm"
          value={harness.provider}
          options={providerOptions(offers)}
          change={provider =>
            dispatch({type: 'set-harness', harness: withProvider(offers, harness, provider)})
          }
        />
      </Field>
      <Field label="Model" hideLabel>
        <Select
          size="sm"
          value={harness.model}
          options={modelOptions(offers, harness.provider)}
          change={model =>
            dispatch({type: 'set-harness', harness: {provider: harness.provider, model}})
          }
        />
      </Field>
    </Row>
  );
}

function Header(props: ChatPanelView): JSX.Element {
  const {threads, shown, dispatch} = props;
  const current = threads.find(entry => entry.id === shown);
  return (
    <Stack gap="space-3">
      <MenuBounds>
        <Row justify="between" align="start" gap="space-3" wrap>
          <Stack gap="space-1">
            <Text as="h2" variant="heading">
              Chat
            </Text>
            {current === undefined ? null : (
              <Text variant="small" tone="muted">
                {current.title}
              </Text>
            )}
          </Stack>
          <Row gap="space-2">
            {threads.length === 0 ? null : (
              <Menu
                text="Threads"
                variant="quiet"
                size="sm"
                align="end"
                items={threads.map(entry => ({
                  id: entry.id,
                  label: entry.title,
                  press: () => dispatch({type: 'choose-thread', id: entry.id}),
                }))}
              />
            )}
            {props.canAsk ? (
              <Button
                variant="quiet"
                size="sm"
                text="New chat"
                press={() => dispatch({type: 'new-chat'})}
              />
            ) : null}
            <Button
              variant="quiet"
              size="sm"
              icon="cross"
              label="Close"
              press={() => dispatch({type: 'close'})}
            />
          </Row>
        </Row>
      </MenuBounds>
      {shown === null && props.canAsk ? <Picker panel={props.panel} dispatch={dispatch} /> : null}
    </Stack>
  );
}

function row(id: string, node: JSX.Element): UiThreadItem {
  return {kind: 'node', id, node};
}

function uiItem(item: PanelItem, roles: readonly string[]): UiThreadItem {
  switch (item.kind) {
    case 'question':
      return row(item.id, <ChatMessage side="end" text={item.text} />);
    case 'answer':
      return row(
        item.id,
        <ChatMessage
          side="start"
          text={item.text}
          {...(item.streaming ? {notice: 'Writing'} : {})}
        />,
      );
    case 'waiting':
      return row(
        item.id,
        <Text variant="small" tone="muted">
          Working
        </Text>,
      );
    case 'card':
      return row(
        item.card.id,
        <SteerCardView
          draft={item.card.draft}
          view={item.card.view}
          actions={item.card.actions}
          roles={roles}
        />,
      );
  }
}

/** One input and one send control; New steer turns what is typed into a steer card. */
function Composer(props: ChatPanelView): JSX.Element {
  const {panel, dispatch} = props;
  const submit = (): void => dispatch({type: 'submit', threadId: props.shown});
  return (
    <Stack gap="space-3">
      {panel.error === null ? null : <FailureNote failure={panel.error} />}
      <Field label="Message" hideLabel>
        <TextInput
          value={panel.composer}
          rows={2}
          placeholder="Ask about this run"
          change={text => dispatch({type: 'set-composer', text})}
          submit={submit}
        />
      </Field>
      <Row justify="between" gap="space-3">
        {props.readOnly ? (
          <span />
        ) : (
          <Button
            variant="quiet"
            size="sm"
            icon="plus"
            text="New steer"
            press={() => dispatch({type: 'new-steer'})}
          />
        )}
        <Button variant="primary" size="sm" icon="arrow-up" label="Ask" press={submit} />
      </Row>
    </Stack>
  );
}

/**
 * The question a thread opens at: its latest, so the exchange the reader came back to starts whole
 * at the top edge instead of at the end of the answer, which cuts the question by however much the
 * answer overflows. None while something is being written or waited on: that thread follows its end.
 */
function opensAt(items: readonly PanelItem[]): string | null {
  const writing = items.some(
    item => item.kind === 'waiting' || (item.kind === 'answer' && item.streaming),
  );
  if (writing) return null;
  return [...items].reverse().find(item => item.kind === 'question')?.id ?? null;
}

function Panel(props: ChatPanelView): JSX.Element {
  const items = props.items.map(item => uiItem(item, props.roles));
  const opens = opensAt(props.items);
  return (
    <Stack gap="space-3" grow>
      <Header {...props} />
      {items.length === 0 ? (
        <Stack gap="space-3" grow>
          {null}
        </Stack>
      ) : (
        <Thread
          key={props.shown ?? 'new'}
          label="Chat"
          items={items}
          size="fill"
          plain
          toEnd={items.length}
          {...(opens === null ? {} : {opensAt: opens})}
        />
      )}
      {props.canAsk ? (
        <Composer {...props} />
      ) : (
        <Text variant="small" tone="muted">
          Read only
        </Text>
      )}
    </Stack>
  );
}

/**
 * The chat panel: header, harness picker for a new chat, the thread, and the composer. A failure
 * to draw it stays in the panel.
 */
export function ChatPanel(props: ChatPanelView): JSX.Element {
  return (
    <ChatBoundary resetKey={props.shown} close={() => props.dispatch({type: 'close'})}>
      <Panel {...props} />
    </ChatBoundary>
  );
}
