/**
 * The Stop control for one agent, by what a person does: a stoppable agent offers Stop agent; a
 * press opens a modal that says what a stop costs; Cancel takes it back; the confirmation carries the
 * press; a host refusal is read back in the host's words; and while one is in flight the press is
 * held. The lead and a finished agent offer nothing. `canStopAgent` is the pure predicate behind the
 * showing, pinned on its boundaries.
 */
import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  type Agent,
  AgentRole,
  AgentSchema,
  AgentStatus,
  AgentStopStage,
  CommandSchema,
  ReceiptSchema,
  Refusal,
} from '@vibesys/orchestrator-proto';
import type {CommandProgress} from '../../state/index.js';
import {installDom, mount, type Screen} from '../../testing/dom/harness.js';
import {canStopAgent, StopControl} from './stop-control.js';

installDom();

function agentOf(
  parts: {
    readonly role?: AgentRole;
    readonly status?: AgentStatus;
    readonly stopStage?: AgentStopStage;
  } = {},
): Agent {
  return create(AgentSchema, {
    id: 'agent-1',
    role: parts.role ?? AgentRole.WORKER,
    status: parts.status ?? AgentStatus.ACTIVE,
    stopStage: parts.stopStage ?? AgentStopStage.NONE,
  });
}

const refused = (refusal: Refusal): CommandProgress => ({
  _tag: 'Refused',
  receipt: create(ReceiptSchema, {commandId: 'c1', refusal}),
});
const sending: CommandProgress = {
  _tag: 'Sending',
  command: create(CommandSchema, {
    id: 'c1',
    body: {case: 'stopAgent', value: {agentId: 'agent-1'}},
  }),
};

function render(
  agent: Agent,
  progress: CommandProgress | undefined,
  stop: () => void = () => undefined,
  retry: () => void = () => undefined,
): Screen {
  return mount(<StopControl agent={agent} progress={progress} stop={stop} retry={retry} />);
}

/** The modal's `<dialog>`, which holds the confirm action and the outcome. */
function sheet(screen: Screen): HTMLElement {
  const element = screen.container.querySelector<HTMLElement>('dialog.vs-dialog');
  if (element === null) throw new Error('the dialog is in the tree');
  return element;
}

/** The one button named `name` inside the dialog (its Stop agent, its Cancel, its Close). */
function inSheet(screen: Screen, name: string): HTMLElement {
  const found = [...sheet(screen).querySelectorAll<HTMLElement>('button')].filter(
    button => (button.getAttribute('aria-label') ?? button.textContent?.trim()) === name,
  );
  const [only] = found;
  if (found.length !== 1 || only === undefined)
    throw new Error(`one ${JSON.stringify(name)} in the dialog, found ${found.length}`);
  return only;
}

const ROLES = [
  AgentRole.UNSPECIFIED,
  AgentRole.LEAD,
  AgentRole.PLANNER,
  AgentRole.WORKER,
  AgentRole.JUDGE,
  AgentRole.COMBINER,
  AgentRole.PROFILER,
];
const LIVE = [AgentStatus.LAUNCHING, AgentStatus.ACTIVE, AgentStatus.WAITING, AgentStatus.IDLE];
const OVER = [AgentStatus.ENDED, AgentStatus.FAILED, AgentStatus.LOST];

describe('whether an agent can be stopped', () => {
  it('stops any live role but the lead', () => {
    for (const role of ROLES) expect(canStopAgent(agentOf({role}))).toBe(role !== AgentRole.LEAD);
  });

  it('stops an agent that is still live, including one only waiting or idle, and never one that is over', () => {
    for (const status of LIVE) expect(canStopAgent(agentOf({status}))).toBe(true);
    for (const status of OVER) expect(canStopAgent(agentOf({status}))).toBe(false);
  });

  it('still stops an agent that is only interrupting, but not one already closing its session', () => {
    expect(canStopAgent(agentOf({stopStage: AgentStopStage.INTERRUPTING}))).toBe(true);
    expect(canStopAgent(agentOf({stopStage: AgentStopStage.CLOSING}))).toBe(false);
  });
});

describe('the stop control', () => {
  it('offers a danger Stop agent for a stoppable agent, and nothing for the lead or a finished agent', () => {
    const live = render(agentOf({}), undefined);
    const button = live.control('Stop agent');
    expect(button.getAttribute('data-variant')).toBe('danger');
    expect(button.textContent?.trim()).toBe('Stop agent');
    live.unmount();

    for (const agent of [agentOf({role: AgentRole.LEAD}), agentOf({status: AgentStatus.ENDED})]) {
      const none = render(agent, undefined);
      expect(none.controls('Stop agent')).toHaveLength(0);
      expect(none.text()).toBe('');
      none.unmount();
    }
  });

  it('a press opens a modal that says the turn ends and the session closes, and the run keeps going', () => {
    const screen = render(agentOf({}), undefined);
    expect(sheet(screen).textContent ?? '').not.toContain('Stop this agent?');
    screen.press(screen.control('Stop agent'));
    const text = sheet(screen).textContent ?? '';
    expect(text).toContain('Stop this agent?');
    expect(text).toContain('its session closes');
    expect(text).toContain('The run keeps going');
    expect(inSheet(screen, 'Cancel')).toBeDefined();
    expect(inSheet(screen, 'Stop agent')).toBeDefined();
    screen.unmount();
  });

  it('takes the stop back on Cancel, issuing nothing and closing the modal', () => {
    let stops = 0;
    const screen = render(agentOf({}), undefined, () => {
      stops += 1;
    });
    screen.press(screen.control('Stop agent'));
    screen.press(inSheet(screen, 'Cancel'));
    expect(stops).toBe(0);
    expect(sheet(screen).textContent ?? '').not.toContain('Stop this agent?');
    screen.unmount();
  });

  it('carries the press on the confirmation, once', () => {
    let stops = 0;
    const screen = render(agentOf({}), undefined, () => {
      stops += 1;
    });
    screen.press(screen.control('Stop agent'));
    screen.press(inSheet(screen, 'Stop agent'));
    expect(stops).toBe(1);
    screen.unmount();
  });

  it('reads a host refusal in the host’s own words and leaves only a way to close', () => {
    const screen = render(agentOf({}), refused(Refusal.AGENT_NOT_LIVE));
    screen.press(screen.control('Stop agent'));
    const text = sheet(screen).textContent ?? '';
    expect(text).toContain('refused');
    expect(text).toContain('Agent finished');
    expect(inSheet(screen, 'Close')).toBeDefined();
    expect(screen.controls('Cancel')).toHaveLength(0);
    screen.unmount();
  });

  it('holds the press while a stop is in flight: the button reads Stopping and does nothing', () => {
    let stops = 0;
    const screen = render(agentOf({}), undefined, () => {
      stops += 1;
    });
    screen.press(screen.control('Stop agent'));
    screen.render(
      <StopControl
        agent={agentOf({})}
        progress={sending}
        stop={() => {
          stops += 1;
        }}
        retry={() => undefined}
      />,
    );
    const confirm = inSheet(screen, 'Stop agent');
    expect(confirm.textContent?.trim()).toBe('Stopping agent');
    expect(sheet(screen).textContent ?? '').toContain('Stop agent: sending');
    expect(confirm.getAttribute('aria-disabled')).toBe('true');
    screen.press(confirm);
    expect(stops).toBe(0);
    screen.unmount();
  });
});
