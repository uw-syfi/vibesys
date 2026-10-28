/**
 * "Use a different agent for a role", in Review's Agent rows, closed until it is opened (or while
 * a role already has its own agent). Each role is one grouped model select (maker, then generation
 * or family, then model) and a harness select; a role stays on the run's agent until changed.
 */
import type {HarnessOffer} from '@vibesys/ipc';
import {Collapsible, Field, Grid, Row, Select, Stack, Text} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  AGENT_ENVIRONMENT,
  type AgentChoice,
  defaultRole,
  harnessLabel,
  offers as helperOffers,
  modelLabel,
  pickedRoleAgent,
  roleAgent,
  roleHarnesses,
  roleLabel,
  roleModels,
  roleProviders,
  roleSummary,
  rolesWaitWords,
} from '../../state/index.js';
import type {CardProps} from './card-props.js';
import {NEEDS_UPDATE, roleAgentsChanged, rolesView} from './view.js';

/** The select's value for a role that follows the run. */
const RUN = '';

function RoleRow(
  props: CardProps & {
    readonly offers: readonly HarnessOffer[];
    readonly agent: AgentChoice;
    readonly role: string;
  },
): JSX.Element {
  const {dispatch, offers, agent, role} = props;
  const choice = agent.roles.find(each => each.role === role) ?? defaultRole(role);
  const label = roleLabel(role);
  const does = roleSummary(role);
  const run = {provider: agent.provider, model: agent.model};
  const own = roleAgent(agent, choice);
  const providers = roleProviders(offers, agent.provider, AGENT_ENVIRONMENT);
  const models = roleModels(offers, providers, own.provider, choice.agent === null ? run : null);
  const harnesses = roleHarnesses(offers, providers, own.model);
  const set = (next: ReturnType<typeof pickedRoleAgent>): void =>
    dispatch({type: 'role', role: {...choice, agent: next}});
  return (
    <Grid min="field" gap="space-3">
      <Stack gap="space-1">
        <Text variant="body-strong">{label}</Text>
        {does === null ? null : (
          <Text variant="small" tone="muted">
            {does}
          </Text>
        )}
      </Stack>
      <Field label={`${label} model`}>
        <Select
          value={choice.agent === null ? RUN : JSON.stringify([own.provider, own.model])}
          options={[
            {value: RUN, label: `Same as the run (${modelLabel(offers, run.provider, run.model)})`},
            ...models.map(model => ({value: model.key, label: model.label, group: model.group})),
          ]}
          change={key => {
            const picked = models.find(model => model.key === key);
            set(
              picked === undefined
                ? null
                : pickedRoleAgent(run, {provider: picked.provider, model: picked.model}),
            );
          }}
        />
      </Field>
      {harnesses.length > 1 ? (
        <Field label={`${label} harness`}>
          <Select
            value={own.provider}
            options={harnesses.map(provider => ({
              value: provider,
              label: harnessLabel(offers, provider),
            }))}
            change={provider => set(pickedRoleAgent(run, {provider, model: own.model}))}
          />
        </Field>
      ) : null}
    </Grid>
  );
}

const HEADING = 'Use a different agent for a role';
const CHECKING = 'Checking';

/**
 * The roles' heading with one short status, in place of the roles a machine cannot offer: the
 * status belongs to this row, never to the harness tiles above it.
 */
function RoleStatus(props: {readonly status: string}): JSX.Element {
  return (
    <Row justify="between" wrap>
      <Text variant="small">{HEADING}</Text>
      <Text variant="small" tone="muted">
        {props.status}
      </Text>
    </Row>
  );
}

/** The roles' toggle and rows, or the status of why there are none to show. */
export function RoleAgents(
  props: CardProps & {readonly offers: readonly HarnessOffer[]; readonly agent: AgentChoice},
): JSX.Element {
  const {context} = props;
  const changed = roleAgentsChanged(context).length;
  const [open, setOpen] = useState(changed > 0);
  const view = rolesView(context);
  const machine = context.form.machine;
  if (!helperOffers(context.helper, machine, 'run_role_agents')) {
    return <RoleStatus status={context.helper?.for === machine ? NEEDS_UPDATE : CHECKING} />;
  }
  if (view.kind === 'unsupported') return <RoleStatus status={NEEDS_UPDATE} />;
  if (view.kind === 'waiting') return <RoleStatus status={rolesWaitWords(view.on).short} />;
  if (view.kind === 'pending') return <RoleStatus status={CHECKING} />;
  return (
    <Collapsible
      label={HEADING}
      open={open}
      toggle={setOpen}
      {...(open
        ? {}
        : {
            summary:
              changed === 0
                ? 'Same as the run'
                : `${changed} ${changed === 1 ? 'role' : 'roles'} changed`,
          })}
    >
      <Stack gap="space-4">
        {view.roles.map(role => (
          <RoleRow key={role} {...props} role={role} />
        ))}
      </Stack>
    </Collapsible>
  );
}
