import {
  Button,
  EmptyState,
  Field,
  Menu,
  Row,
  SegmentedControl,
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
  choiceLabel,
  modelOptions,
  type PanelItem,
  providerOptions,
  withProvider,
} from '../../state/index.js';
import {SteerCardView} from './steer-card-view.js';

function Picker(props: Pick<ChatPanelView, 'panel' | 'dispatch'>): JSX.Element | null {
  const {panel, dispatch} = props;
  const {catalogue, harness} = panel;
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
    <Row gap="space-3">
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
  const {catalogue} = props.panel;
  const harness =
    catalogue.kind === 'listed' ? choiceLabel(catalogue.offers, current ?? null) : null;
  return (
    <Stack gap="space-2">
      <Row justify="between" gap="space-3">
        <Text as="h2" variant="heading">
          Chat
        </Text>
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
          <Button
            variant="quiet"
            size="sm"
            text="New chat"
            press={() => dispatch({type: 'new-chat'})}
          />
          <Button variant="quiet" size="sm" text="Close" press={() => dispatch({type: 'close'})} />
        </Row>
      </Row>
      {shown === null ? (
        <Picker panel={props.panel} dispatch={dispatch} />
      ) : (
        <Text variant="small" tone="muted">
          {[current?.title, harness].filter(Boolean).join(' · ')}
        </Text>
      )}
    </Stack>
  );
}

function uiItem(item: PanelItem): UiThreadItem {
  switch (item.kind) {
    case 'question':
      return {kind: 'message', id: item.id, author: 'You', variant: 'note', text: item.text};
    case 'answer':
      return {
        kind: 'message',
        id: item.id,
        author: 'Agent',
        variant: 'reply',
        text: item.text,
        ...(item.streaming ? {notice: 'Writing'} : {}),
      };
    case 'waiting':
      return {
        kind: 'node',
        id: item.id,
        node: (
          <Text variant="small" tone="muted">
            Working
          </Text>
        ),
      };
    case 'card':
      return {
        kind: 'node',
        id: item.card.id,
        node: (
          <SteerCardView
            draft={item.card.draft}
            view={item.card.view}
            actions={item.card.actions}
          />
        ),
      };
  }
}

function Composer(props: ChatPanelView): JSX.Element {
  const {panel, busy, dispatch} = props;
  const submit = (): void => dispatch({type: 'submit', threadId: props.shown});
  const asking = panel.composeAs === 'ask';
  return (
    <Stack gap="space-3">
      {panel.error === null ? null : (
        <Text variant="small" tone="err">
          {panel.error}
        </Text>
      )}
      <Field label="Message" hideLabel>
        <TextInput
          value={panel.composer}
          rows={3}
          placeholder={asking ? 'Ask' : 'Steer'}
          change={text => dispatch({type: 'set-composer', text})}
          submit={submit}
        />
      </Field>
      <Row justify="between" gap="space-3">
        <SegmentedControl
          label="Compose as"
          size="xs"
          options={[
            {value: 'ask', label: 'Ask'},
            {value: 'steer', label: 'Steer'},
          ]}
          value={panel.composeAs}
          change={as => dispatch({type: 'compose-as', as})}
        />
        <Button
          variant="primary"
          size="sm"
          text={asking ? 'Ask' : 'Steer'}
          press={submit}
          {...(busy && asking ? {disabledReason: 'Working'} : {})}
        />
      </Row>
    </Stack>
  );
}

/** The chat panel: header, harness picker for a new chat, the thread, and the composer. */
export function ChatPanel(props: ChatPanelView): JSX.Element {
  const items = props.items.map(uiItem);
  return (
    <Stack gap="space-3" grow>
      <Header {...props} />
      <Stack gap="space-3" grow>
        {items.length === 0 ? (
          <EmptyState title="No messages" />
        ) : (
          <Thread key={props.shown ?? 'new'} label="Chat" items={items} size="fill" />
        )}
      </Stack>
      <Composer {...props} />
    </Stack>
  );
}
