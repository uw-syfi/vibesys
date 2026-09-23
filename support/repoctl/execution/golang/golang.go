// Package golang plans policy-configured Go checks.
package golang

import "repoctl/execution"

type Planner struct{}

func (Planner) Plan(suite execution.Suite, _ []string) ([]execution.Check, error) {
	return execution.Commands(suite)
}
