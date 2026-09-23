// Package rust plans policy-configured Rust checks.
package rust

import "repoctl/execution"

type Planner struct{}

func (Planner) Plan(suite execution.Suite, _ []string) ([]execution.Check, error) {
	return execution.Commands(suite)
}
