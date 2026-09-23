// Package python plans policy-configured Python checks.
package python

import "repoctl/execution"

type Planner struct{}

func (Planner) Plan(suite execution.Suite, _ []string) ([]execution.Check, error) {
	return execution.Commands(suite)
}
