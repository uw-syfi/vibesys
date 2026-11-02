/**
 * Whether the run's agent CLI is signed in on the machine, as the machine's description says
 * (`MachineDescription.harness_logins`, the probe Settings › Agent logins also reads). Only a CLI
 * that says it is signed out stops a start; a machine that cannot tell says nothing.
 */
import type {AgentLogin} from '@vibesys/contracts/host-helper';
import type {FactsFor, Form} from './form.js';

export type LoginState = AgentLogin['state'];

/**
 * The login of the agent's CLI on the form's machine; null while the machine has not described
 * itself, no agent is chosen, the CLI is not installed there, or its VibeSys predates the fact.
 */
export function agentLogin(facts: FactsFor | null, form: Form): LoginState | null {
  if (facts?.for !== form.machine || facts.value.kind !== 'offered' || form.agent === null) {
    return null;
  }
  return facts.value.value.harness_logins?.[form.agent.provider] ?? null;
}
