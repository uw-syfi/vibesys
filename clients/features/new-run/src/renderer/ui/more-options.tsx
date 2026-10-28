/**
 * More options: what most runs leave alone, behind one disclosure that is closed until the user
 * opens it (and then stays as they left it, per machine). A closed disclosure still says what it
 * holds that is not the default, and opens by itself while something in it is wrong.
 */

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
  type NewRunContext,
  roleLabel,
  roleSummary,
} from '../../state/index.js';
import type {CardProps} from './card-props.js';
import {destinationTaken, moreOptionsSummary, type RolesView, rolesView} from './view.js';

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

function RoleRows(props: CardProps & {readonly roles: readonly string[]}): JSX.Element | null {
  const {context, dispatch, roles} = props;
  const agent = context.form.agent;
  if (agent === null) return null;
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
    role => role.effort !== 'default',
  ).length;
  return changed === 0
    ? 'Reasoning effort: every role default'
    : `Reasoning effort: ${changed} changed`;
}

/** The role table's toggle and rows, or why the run's roles are not listed. */
function RolesSection(props: CardProps): JSX.Element {
  const {context} = props;
  const [shown, setShown] = useState(false);
  const view: RolesView = rolesView(context);
  if (view.kind === 'unsupported') {
    return (
      <Text variant="small" tone="muted">
        This machine's VibeSys does not report its agent roles, so every role runs as the run.
        Update VibeSys on it to set a reasoning effort per role.
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
    <Field label="Create the example at" {...(destinationTaken(context) ? {error: 'Taken'} : {})}>
      <TextInput
        mono
        value={project.destination}
        change={destination => dispatch({type: 'destination', destination})}
      />
    </Field>
  );
}

/** More options: results, the reasoning effort of each role, where an example is copied to, and extra arguments. */
export function MoreOptions(props: CardProps): JSX.Element {
  const {context, dispatch} = props;
  const {machine, advanced, budget} = context.form;
  const problem = advancedProblem(advanced);
  // The user's choice for this machine; until they make one, open only while something is wrong.
  const open = context.moreOpen[machine] ?? (problem !== null || destinationTaken(context));
  return (
    <Collapsible
      label="More options"
      open={open}
      toggle={next => dispatch({type: 'more_options', machine, open: next})}
      {...(open ? {} : {summary: problem ?? moreOptionsSummary(context)})}
    >
      <Stack gap="space-4">
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
        <Field label="Extra arguments" {...(problem === null ? {} : {error: problem})}>
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
