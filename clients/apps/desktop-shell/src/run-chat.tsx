/**
 * The Run screen with its chat: composition of the run page and the run chat, which are peers.
 * The chat reaches the run through the page's attached session (`page.chat`) and the host's
 * harness catalogue through the welcome bridge; it reads the run from the page's fold.
 */
import {ChatPanel, type RunChatPorts, useRunChat} from '@vibesys/feature-run-chat/renderer';
import {RunScreen, type useRunPage} from '@vibesys/feature-run-page/renderer';
import {ideaOfAttempt} from '@vibesys/feature-run-page/state';
import {settled, type WelcomeBridge} from '@vibesys/ipc';
import type {RunRef} from '@vibesys/runs';
import {type JSX, useMemo} from 'react';

type RunPageView = NonNullable<ReturnType<typeof useRunPage>>;

function chatPorts(page: RunPageView['chat'], bridge: WelcomeBridge, run: RunRef): RunChatPorts {
  return {
    harnesses: async () => {
      const result = await settled(bridge.harnesses(run.host));
      return result.ok ? result.value : {kind: 'unsupported'};
    },
    createThread: (harness, signal) => page.createThread(harness, signal),
    ask: (input, signal) => page.ask(input, signal),
    control: request => page.control(request),
    newControlId: () => crypto.randomUUID(),
  };
}

/** The Run screen over `page`, with the Chat button and its panel. */
export function RunWithChat(props: {
  readonly page: RunPageView;
  readonly run: RunRef;
  readonly bridge: WelcomeBridge;
}): JSX.Element {
  const {page, run, bridge} = props;
  const ports = useMemo(() => chatPorts(page.chat, bridge, run), [page.chat, bridge, run]);
  const ideaOf = useMemo(() => ideaOfAttempt(page.whole.candidates.candidates), [page.whole]);
  const roles = useMemo(() => page.whole.agents.agents.map(agent => agent.role), [page.whole]);
  const chat = useRunChat({
    identity: `${run.host} ${run.runId}`,
    core: page.core,
    ports,
    ideaOf,
    roles,
  });
  return (
    <RunScreen
      state={page.state}
      model={page.model}
      whole={page.whole}
      core={page.core}
      route={page.route}
      zoom={page.zoom}
      dispatch={page.dispatch}
      modelNames={page.modelNames}
      chat={{
        open: chat.open,
        toggle: chat.toggle,
        panel: chat.panel === null ? null : <ChatPanel {...chat.panel} />,
      }}
    />
  );
}
