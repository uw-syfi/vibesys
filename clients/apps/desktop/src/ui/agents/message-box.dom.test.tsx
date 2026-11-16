/**
 * The box a person writes a message to an agent in, by what a person does: typing fills it, a press
 * of Send carries the text and shows where the message stands, and the words sent stay on screen
 * even once the host has accepted the message and the box itself has emptied for the next one.
 */
import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  type Agent,
  AgentRole,
  AgentSchema,
  AgentStatus,
  Delivery,
  ReceiptSchema,
} from '@vibesys/orchestrator-proto';
import {act} from 'react';
import type {CommandProgress} from '../../state/index.js';
import {installDom, mount, type Screen} from '../../testing/dom/harness.js';
import {MessageBox} from './message-box.js';

installDom();

function agentOf(status: AgentStatus = AgentStatus.ACTIVE): Agent {
  return create(AgentSchema, {id: 'agent-1', role: AgentRole.WORKER, status});
}

/** Types into a field as a person does: focused, the native value setter, then input and change events. */
function type(field: HTMLTextAreaElement, text: string): void {
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

/** The textarea a person writes the message in. */
function box(screen: Screen): HTMLTextAreaElement {
  const found = screen.container.querySelector<HTMLTextAreaElement>(
    'textarea[placeholder="Write a message to this agent"]',
  );
  if (found === null) throw new Error('the message field is drawn');
  return found;
}

function render(agent: Agent, progress: CommandProgress | undefined): Screen {
  return mount(
    <MessageBox
      nowMs={0}
      agent={agent}
      progress={progress}
      sent={[]}
      send={() => undefined}
      retry={() => undefined}
    />,
  );
}

describe('the message box', () => {
  it('keeps the words a person sent on screen once the host has accepted them, even once the box has emptied', () => {
    const agent = agentOf();
    const screen = render(agent, undefined);
    type(box(screen), 'Please check the config file.');
    screen.press(screen.control('Send message'));
    const receipt = create(ReceiptSchema, {
      commandId: 'c1',
      messageId: 'm1',
      delivery: Delivery.QUEUED,
    });
    const accepted: CommandProgress = {_tag: 'Accepted', receipt};
    act(() => {
      screen.render(
        <MessageBox
          nowMs={0}
          agent={agent}
          progress={accepted}
          sent={[]}
          send={() => undefined}
          retry={() => undefined}
        />,
      );
    });
    // The box clears once the host has taken the message; its words must not go with it.
    expect(box(screen).value).toBe('');
    expect(screen.text()).toContain('Please check the config file.');
    expect(screen.text()).not.toContain('Last message');
    screen.unmount();
  });
});
