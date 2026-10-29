import {ChatPanel, type RunChatPorts, useRunChat} from '@vibesys/feature-run-chat/renderer';
import {runHarness} from '@vibesys/feature-run-chat/state';
import {
  type ChatServicePorts,
  chatService,
  type RunRecords,
  useChatThreads,
  useSteerDeliveries,
} from '@vibesys/feature-run-sessions/renderer';
import {sessionCore} from '@vibesys/feature-run-sessions/state';
import {newRequestId} from '@vibesys/request-identity';
import type {CoreState} from '@vibesys/run-state';
import {chatControlIntent, createdSession, type HarnessOption} from '@vibesys/runs/chat';
import {addressedIntent, runReference} from '@vibesys/runs/intents';
import {Button, Stack, Text} from '@vibesys/ui';
import {useEffect, useMemo} from 'react';

function harnessEntry(option: HarnessOption) {
  return {
    provider: option.provider,
    model: option.model,
    default: option.default,
    ...(option.display
      ? {
          display: {
            providerName: option.display.providerName,
            ...(option.display.modelName ? {modelName: option.display.modelName} : {}),
          },
        }
      : {}),
  };
}

/** One abort signal per owner, cancelled when the owner changes or unmounts. */
function useLifetime(owner: object): AbortController {
  const lifetime = useMemo(() => ({owner, controller: new AbortController()}), [owner]);
  useEffect(() => () => lifetime.controller.abort(), [lifetime]);
  return lifetime.controller;
}

/** The chat's state over the gateway: the panel's host, and what its thread history says. */
export function useGatewayChatState(
  input: ChatServicePorts & {
    core: CoreState | null;
    epoch: bigint;
    readOnly: boolean;
    records: RunRecords;
  },
) {
  const ports = useMemo<ChatServicePorts>(
    () => ({
      bridge: input.bridge,
      host: input.host,
      runId: input.runId,
      projectRoot: input.projectRoot,
      schedule: input.schedule,
    }),
    [input.bridge, input.host, input.runId, input.projectRoot, input.schedule],
  );
  const service = useMemo(() => chatService(ports), [ports]);
  const threads = useChatThreads(ports, input.projectRoot !== '');
  const steers = useSteerDeliveries(input.records, input.projectRoot !== '');
  const lifetime = useLifetime(ports);
  const chatPorts: RunChatPorts = {
    harnesses: async () => ({
      kind: 'listed',
      harnesses: (await service.catalog(newRequestId(), lifetime.signal)).map(harnessEntry),
    }),
    createThread: async (harness, signal, requestId) => {
      try {
        const choice = harness ?? (input.core ? runHarness(input.core) : null);
        if (!choice) return {ok: false, message: 'Choose a harness and model'};
        const threadId = await service.create(`thread-${requestId}`, choice, signal);
        threads.created(createdSession(threadId, choice));
        threads.refresh();
        return {ok: true, threadId};
      } catch (error) {
        return {ok: false, message: String(error)};
      }
    },
    ask: async (request, signal) => {
      try {
        await service.send(request.threadId, request.requestId, request.text, signal);
        return {ok: true};
      } catch (error) {
        return {ok: false, message: String(error)};
      }
    },
    control: async request => {
      try {
        await service.control(
          addressedIntent(
            runReference(input.projectRoot, input.runId),
            request.control_id,
            input.epoch,
            chatControlIntent(request),
          ),
          lifetime.signal,
        );
        return {kind: 'acknowledged'};
      } catch (error) {
        return {kind: 'rejected', message: String(error)};
      }
    },
    newControlId: newRequestId,
  };
  const core = input.core
    ? {...sessionCore(input.core, threads.context), steerDeliveries: steers}
    : null;
  const roles = useMemo(
    () => [...new Set(input.core?.usageRecords.map(record => String(record.role)) ?? [])],
    [input.core?.usageRecords],
  );
  const chat = useRunChat({
    identity: `${input.host}/${input.runId}`,
    core,
    roles,
    ports: chatPorts,
    readOnly: input.readOnly,
    askable: true,
  });
  useEffect(() => {
    if (chat.open) threads.refresh();
  }, [chat.open, threads.refresh]);
  const shown = chat.open ? (chat.panel?.shown ?? null) : null;
  useEffect(() => threads.select(shown), [shown, threads.select]);
  return {
    chat,
    error: threads.context?.historyError ?? threads.context?.listError ?? null,
    reload: threads.reload,
  };
}

/** Compose the existing chat UI with protobuf gateway operations and session records. */
export function useGatewayChat(input: Parameters<typeof useGatewayChatState>[0]) {
  const {chat, error, reload} = useGatewayChatState(input);
  return {
    ...chat,
    panel: chat.panel ? (
      <Stack gap="space-2" grow>
        {error ? (
          <Stack gap="space-1">
            <Text tone="err" title={error}>
              Unavailable
            </Text>
            <Button text="Retry history" press={reload} />
          </Stack>
        ) : null}
        <ChatPanel {...chat.panel} />
      </Stack>
    ) : null,
  };
}
