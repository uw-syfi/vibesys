/**
 * More options: what most runs leave alone, behind one disclosure that is closed until the user
 * opens it (and then stays as they left it, per machine). A closed disclosure still says what it
 * holds that is not the default, and opens by itself while something in it is wrong.
 */

import type {HarnessOffer} from '@vibesys/ipc';
import {
  Button,
  Collapsible,
  Field,
  Grid,
  Row,
  SegmentedControl,
  Select,
  Stack,
  Text,
  TextInput,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  advancedProblem,
  type Budget,
  defaultRole,
  type Effort,
  modelLabel,
  type NewRunContext,
  roleLabel,
  roleSummary,
  vettedModels,
} from '../../state/index.js';
import {TimeLimitField} from './budget-fields.js';
import type {CardProps} from './card-props.js';
import {moreOptionsSummary, type RolesView, rolesView} from './view.js';

const NONE = '';

const RESULTS: readonly {readonly value: Budget['results']; readonly label: string}[] = [
  {value: 'keep', label: 'Keep on the machine'},
  {value: 'publish_github', label: 'Also publish to GitHub'},
];

const EFFORTS: readonly {readonly value: Effort; readonly label: string}[] = [
  {value: 'default', label: 'Default'},
  {value: 'low', label: 'Low'},
  {value: 'medium', label: 'Medium'},
  {value: 'high', label: 'High'},
];

function RoleRows(
  props: CardProps & {readonly offers: readonly HarnessOffer[]; readonly roles: readonly string[]},
): JSX.Element | null {
  const {context, dispatch, offers, roles} = props;
  const agent = context.form.agent;
  if (agent === null) return null;
  const models = vettedModels(offers, agent.provider);
  return (
    <Stack gap="space-3">
      {roles.map(role => {
        const choice = agent.roles.find(each => each.role === role) ?? defaultRole(role);
        const label = roleLabel(role);
        const does = roleSummary(role);
        return (
          <Grid key={role} min="field" gap="space-3">
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
                value={choice.model ?? NONE}
                options={[
                  {value: NONE, label: 'Same as run'},
                  ...models.map(m => ({value: m, label: modelLabel(offers, agent.provider, m)})),
                ]}
                change={model =>
                  dispatch({type: 'role', role: {...choice, model: model === NONE ? null : model}})
                }
              />
            </Field>
            <Field label={`${label} reasoning effort`}>
              <Select
                value={choice.effort}
                options={EFFORTS}
                change={effort => dispatch({type: 'role', role: {...choice, effort}})}
              />
            </Field>
          </Grid>
        );
      })}
    </Stack>
  );
}

function rolesLabel(context: NewRunContext): string {
  const changed = (context.form.agent?.roles ?? []).filter(
    role => role.model !== null || role.effort !== 'default',
  ).length;
  return changed === 0 ? 'Roles: every role as the run' : `Roles: ${changed} changed`;
}

/** The role table's toggle and rows, or why the run's roles are not listed. */
function RolesSection(props: CardProps & {readonly offers: readonly HarnessOffer[]}): JSX.Element {
  const {context} = props;
  const [shown, setShown] = useState(false);
  const view: RolesView = rolesView(context);
  if (view.kind === 'unsupported') {
    return (
      <Text variant="small" tone="muted">
        This machine's VibeSys does not report its agent roles, so every role runs as the run.
        Update VibeSys on it to set a model per role.
      </Text>
    );
  }
  if (view.kind === 'pending') {
    return (
      <Text variant="small" tone="muted">
        The roles are read from the machine with the first check of these inputs.
      </Text>
    );
  }
  return (
    <Stack gap="space-3">
      <Row>
        <Button
          variant="quiet"
          icon={shown ? 'chevron-down' : 'chevron-right'}
          text={rolesLabel(context)}
          expanded={shown}
          press={() => setShown(!shown)}
        />
      </Row>
      {shown ? <RoleRows {...props} roles={view.roles} /> : null}
    </Stack>
  );
}

function ExampleDestination(props: CardProps): JSX.Element | null {
  const {context, dispatch} = props;
  const project = context.form.project;
  if (project?.source !== 'example') return null;
  return (
    <Field label="Create the example at" help="A new Git repository the run works in.">
      <TextInput
        mono
        value={project.destination}
        change={destination => dispatch({type: 'destination', destination})}
      />
    </Field>
  );
}

/** More options: results, roles, extra arguments and where an example is copied to. */
export function MoreOptions(
  props: CardProps & {readonly offers: readonly HarnessOffer[]},
): JSX.Element {
  const {context, dispatch} = props;
  const {machine, advanced, budget} = context.form;
  const problem = advancedProblem(advanced);
  // The user's choice for this machine; until they make one, open only while something is wrong.
  const open = context.moreOpen[machine] ?? problem !== null;
  return (
    <Collapsible
      label="More options"
      open={open}
      toggle={next => dispatch({type: 'more_options', machine, open: next})}
      {...(open ? {} : {summary: problem ?? moreOptionsSummary(context)})}
    >
      <Stack gap="space-4">
        <TimeLimitField {...props} />
        <Stack gap="space-2">
          <Text variant="body-strong">Results</Text>
          <SegmentedControl
            label="Results"
            options={RESULTS}
            value={budget.results}
            change={results => dispatch({type: 'budget', budget: {...budget, results}})}
          />
        </Stack>
        <RolesSection {...props} />
        <ExampleDestination {...props} />
        <Field
          label="Extra arguments"
          help="Passed to vibesys run as typed; options set above may not be repeated."
          {...(problem === null ? {} : {error: problem})}
        >
          <TextInput
            mono
            value={advanced}
            change={next => dispatch({type: 'advanced', advanced: next})}
            placeholder="--max-in-flight 3"
          />
        </Field>
      </Stack>
    </Collapsible>
  );
}
