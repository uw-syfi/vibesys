package main

import (
	"repoctl/execution"
	"repoctl/execution/golang"
	"repoctl/execution/python"
	"repoctl/execution/rust"
	"repoctl/execution/typescript"
)

var testPlanners = map[string]execution.Planner{
	"go": golang.Planner{}, "rust": rust.Planner{},
	"python": python.Planner{}, "typescript": typescript.Planner{},
}
