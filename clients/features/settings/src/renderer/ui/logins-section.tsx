/**
 * Agent logins on one machine: each harness's CLI is signed in, not signed in, or not installed
 * there, how, and Sign in starts the harness's own login on that machine. VibeSys shows where a
 * credential is held, never the credential.
 */
import type {AgentLogin} from '@vibesys/ipc';
import {Banner, Button, Card, Chip, ErrorActions, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  failureWords,
  type LoginProgress,
  loginWords,
  type MachineSettingsAction,
  type MachineView,
  signInBlocked,
} from '../../state/index.js';
import {Reading} from './reading.js';

type Dispatch = (action: MachineSettingsAction) => void;

function Progress(props: {
  readonly progress: LoginProgress | undefined;
  readonly machine: string;
  readonly login: AgentLogin;
  readonly check: () => void;
}): JSX.Element | null {
  const {progress, machine, login} = props;
  if (progress === undefined || progress.kind === 'starting') return null;
  if (progress.kind === 'failed') {
    const title = `Could not start the login on ${machine}.`;
    const words = failureWords(progress);
    return (
      <Banner
        tone="err"
        title={title}
        {...words}
        actions={
          <ErrorActions copy={[title, words.body, words.hint].filter(Boolean).join('\n\n')} />
        }
      />
    );
  }
  if (progress.kind === 'unavailable') {
    return (
      <Banner
        tone="warn"
        title={
          progress.reason === 'not_installed'
            ? `${login.provider_name} is not installed on ${machine}.`
            : `${login.provider_name} has no login command to start.`
        }
      />
    );
  }
  return (
    <Banner
      tone="ok"
      title={`The login started on ${machine}.`}
      body={progress.instruction ?? `Finish signing in on ${machine}, then check again.`}
      actions={<Button text="Check again" press={props.check} />}
    />
  );
}

function LoginCard(props: {
  readonly host: string;
  readonly machine: string;
  readonly login: AgentLogin;
  readonly progress: LoginProgress | undefined;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {host, machine, login, progress, dispatch} = props;
  const words = loginWords(login);
  const blocked = signInBlocked(login, machine);
  const starting = progress?.kind === 'starting';
  const reason = starting ? `Starting the login on ${machine}.` : blocked;
  return (
    <Card>
      <Stack gap="space-3">
        <Row gap="space-3" justify="between" align="start" wrap>
          <Stack gap="space-1">
            <Row gap="space-3" wrap>
              <Text as="h3" variant="item">
                {login.provider_name}
              </Text>
              <Chip
                dot
                label={words.label}
                tone={words.tone}
                {...(login.detail === '' ? {} : {hint: login.detail})}
              />
            </Row>
            {words.how.text === '' ? null : (
              <Row gap="space-1" wrap>
                <Text tone="muted">{words.how.text}</Text>
                {words.how.identifier === null ? null : (
                  <Text variant="code">{words.how.identifier}</Text>
                )}
              </Row>
            )}
          </Stack>
          <Button
            variant={login.state === 'signed_in' ? 'quiet' : 'primary'}
            text={starting ? 'Starting…' : 'Sign in'}
            hint={reason ?? `Sign in to ${login.provider_name} on ${machine}`}
            {...(reason === null ? {} : {disabledReason: reason, quietReason: true})}
            press={() => dispatch({type: 'sign-in', host, provider: login.provider})}
          />
        </Row>
        {login.state === 'signed_in' || (login.api_key_variables ?? []).length === 0 ? null : (
          <Row gap="space-2" wrap>
            <Text variant="small" tone="muted" title={`Or set an API key on ${machine}`}>
              API key
            </Text>
            {(login.api_key_variables ?? []).map(variable => (
              <Text key={variable} variant="code">
                {variable}
              </Text>
            ))}
          </Row>
        )}
        <Progress
          progress={progress}
          machine={machine}
          login={login}
          check={() => dispatch({type: 'refresh', host, what: 'logins'})}
        />
      </Stack>
    </Card>
  );
}

/** The Agent logins section for `machine`, read from its helper. */
export function LoginsSection(props: {
  readonly host: string;
  readonly machine: string;
  readonly view: MachineView | undefined;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {host, machine, view, dispatch} = props;
  return (
    <Stack>
      <Stack gap="space-1">
        <Text as="h1" variant="title">
          Agent logins
        </Text>
      </Stack>
      <Reading
        what="the agent logins"
        machine={machine}
        reading={view?.logins}
        retry={() => dispatch({type: 'refresh', host, what: 'logins'})}
      >
        {logins => (
          <Stack>
            {logins.map(login => (
              <LoginCard
                key={login.provider}
                host={host}
                machine={machine}
                login={login}
                progress={view?.starts[login.provider]}
                dispatch={dispatch}
              />
            ))}
          </Stack>
        )}
      </Reading>
    </Stack>
  );
}
