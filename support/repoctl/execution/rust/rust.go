// Package rust plans policy-configured Rust checks.
package rust

import "repoctl/execution"

type Planner struct{}

func (Planner) Validate(suite execution.Suite, _ map[string]bool) error {
	return execution.ValidateCommands(suite)
}

func (Planner) Plan(suite execution.Suite, _ []string) ([]execution.Check, error) {
	return execution.Commands(suite)
}
