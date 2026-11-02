package main

import (
	"repoctl/execution"
	"repoctl/execution/docker"
	"repoctl/execution/golang"
	"repoctl/execution/python"
	"repoctl/execution/rust"
	"repoctl/execution/typescript"
)

var testPlanners = map[string]execution.Planner{
	"docker": docker.Planner{}, "go": golang.Planner{}, "rust": rust.Planner{},
	"python": python.Planner{}, "typescript": typescript.Planner{},
}

func localCoverageChecks(root string, g graph, changed, tests []string) ([]execution.Check, error) {
	return python.CoverageChecks(root, changed, tests, g.CheckGroups["python_coverage_floor"], g.CheckGroups["patch_coverage"])
}
