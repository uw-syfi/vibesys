// Package typescript expands selected workspace packages into test checks.
package typescript

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"repoctl/execution"
)

type Planner struct{}

func (Planner) Plan(suite execution.Suite, packages []string) ([]execution.Check, error) {
	if len(packages) == 0 {
		return execution.Commands(suite)
	}
	checks, err := execution.Always(suite)
	if err != nil {
		return nil, err
	}
	packages = append([]string(nil), packages...)
	sort.Strings(packages)
	for _, name := range packages {
		for _, configured := range suite.PackageCommands {
			if len(configured) == 0 || configured[0] == "" {
				return nil, fmt.Errorf("check group %q has an empty package command", suite.Name)
			}
			args := make([]string, len(configured))
			for i, arg := range configured {
				args[i] = strings.ReplaceAll(arg, "{package}", name)
			}
			checks = append(checks, execution.Check{
				Label: suite.Name + ":" + name, Directory: suite.Directory,
				Args: args, Env: suite.Env,
				Timeout: time.Duration(suite.TimeoutSeconds) * time.Second,
			})
		}
	}
	return checks, nil
}
