package main

import (
	"fmt"
	"strings"

	"repoctl/execution"
)

func (g *graph) validateTestSuites(suites []execution.Suite) error {
	collections := map[string]bool{}
	for _, item := range g.Collections {
		collections[item.Name] = true
	}
	for _, suite := range suites {
		if !contains(g.Jobs, suite.Job) {
			return fmt.Errorf("test_suites: unknown job %q", suite.Job)
		}
		if _, exists := g.TestSuites[suite.Job]; exists {
			return fmt.Errorf("test_suites: duplicate job %q", suite.Job)
		}
		if suite.Directory != "." && !validPath(suite.Directory) {
			return fmt.Errorf("test_suites.%s: unsafe directory %q", suite.Job, suite.Directory)
		}
		if suite.TimeoutSeconds <= 0 {
			return fmt.Errorf("test_suites.%s: timeout_seconds must be positive", suite.Job)
		}
		for key := range suite.Env {
			if !envKey.MatchString(key) {
				return fmt.Errorf("test_suites.%s: invalid environment key %q", suite.Job, key)
			}
		}
		switch suite.Language {
		case "go", "rust", "python":
			if suite.Collection != "" || len(suite.PackageCommands) != 0 {
				return fmt.Errorf("test_suites.%s: collection commands require typescript", suite.Job)
			}
		case "typescript":
			if !collections[suite.Collection] || len(suite.PackageCommands) == 0 {
				return fmt.Errorf("test_suites.%s: typescript requires a collection and package_commands", suite.Job)
			}
			for _, args := range suite.PackageCommands {
				found := false
				for _, arg := range args {
					found = found || strings.Contains(arg, "{package}")
				}
				if !found {
					return fmt.Errorf("test_suites.%s: package command requires {package}", suite.Job)
				}
			}
		default:
			return fmt.Errorf("test_suites.%s: unknown language %q", suite.Job, suite.Language)
		}
		if len(suite.Commands) == 0 {
			return fmt.Errorf("test_suites.%s: commands must not be empty", suite.Job)
		}
		for _, commands := range [][][]string{suite.Commands, suite.PackageCommands} {
			for _, args := range commands {
				if len(args) == 0 || strings.TrimSpace(args[0]) == "" {
					return fmt.Errorf("test_suites.%s: empty command", suite.Job)
				}
			}
		}
		g.TestSuites[suite.Job] = suite
	}
	return nil
}
