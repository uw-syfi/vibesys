// Package docker expands selected Dockerfile build contexts into build checks.
package docker

import "repoctl/execution"

// Planner runs a suite's package_commands once per selected build context, with
// {package} replaced by the context directory.
type Planner struct{}

func (Planner) Validate(suite execution.Suite, collections map[string]bool) error {
	return execution.ValidatePackageCommands(suite, collections)
}

func (Planner) Plan(suite execution.Suite, contexts []string) ([]execution.Check, error) {
	return execution.PackageChecks(suite, contexts)
}
