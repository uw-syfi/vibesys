// Package typescript expands selected workspace packages into test checks.
package typescript

import "repoctl/execution"

type Planner struct{}

func (Planner) Validate(suite execution.Suite, collections map[string]bool) error {
	return execution.ValidatePackageCommands(suite, collections)
}

func (Planner) Plan(suite execution.Suite, packages []string) ([]execution.Check, error) {
	return execution.PackageChecks(suite, packages)
}
