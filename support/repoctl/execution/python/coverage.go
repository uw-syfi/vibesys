package python

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"repoctl/execution"
)

// CoverageChecks measures changed source files with explicitly selected own test
// files. It uses CI's coverage configuration and floor checker. It never selects
// a suite or guesses which tests cover a module. Deleted files need no coverage.
func CoverageChecks(root string, changed, tests []string, floor, patch execution.Suite) ([]execution.Check, error) {
	modules := []string{}
	seen := map[string]bool{}
	for _, path := range changed {
		parts := strings.Split(path, "/")
		if !strings.HasSuffix(path, ".py") || containsPart(parts, "tests") || filepath.Base(path) == "conftest.py" {
			continue
		}
		if !containsPart(parts, "src") && !strings.HasPrefix(path, "scripts/") {
			continue
		}
		info, err := os.Stat(filepath.Join(root, filepath.FromSlash(path)))
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return nil, err
		}
		if !info.Mode().IsRegular() {
			return nil, fmt.Errorf("coverage module %q is not a regular file", path)
		}
		if !seen[path] {
			modules = append(modules, path)
			seen[path] = true
		}
	}
	sort.Strings(modules)
	if len(modules) == 0 {
		return nil, fmt.Errorf("--coverage-only: no touched Python source files to measure")
	}
	if len(tests) == 0 {
		return nil, fmt.Errorf("--coverage-only: supply the touched modules' own test files with repeated --coverage-test FILE")
	}
	for _, path := range tests {
		if filepath.IsAbs(path) || filepath.ToSlash(filepath.Clean(path)) != path || strings.HasPrefix(path, "../") || !strings.HasPrefix(filepath.Base(path), "test_") || !strings.HasSuffix(path, ".py") {
			return nil, fmt.Errorf("--coverage-test %q: expected a repository-relative test_*.py file", path)
		}
		if containsPart(strings.Split(path, "/"), "e2e") || containsPart(strings.Split(path, "/"), "slurm_cluster") || containsPart(strings.Split(path, "/"), "minimal_container") {
			return nil, fmt.Errorf("--coverage-test %q: real-tier tests cannot run in the local coverage mode", path)
		}
		info, err := os.Stat(filepath.Join(root, filepath.FromSlash(path)))
		if err != nil {
			return nil, fmt.Errorf("--coverage-test %q: %w", path, err)
		}
		if !info.Mode().IsRegular() {
			return nil, fmt.Errorf("--coverage-test %q: expected a regular file", path)
		}
	}
	// Keep local data separate from CI artifacts and ordinary pytest coverage.
	// The runner executes these in order and stops on the first failure.
	data := "artifacts/repoctl-coverage/data"
	report := "artifacts/repoctl-coverage/report.json"
	xml := "artifacts/repoctl-coverage/report.xml"
	pytest := []string{"uv", "run", "python", "-m", "pytest", "-q", "-n", "0", "--cov-reset", "--cov-report=", "--cov-fail-under=0", "-m", "not native_build and not e2e and not real_contract and not slurm_cluster and not minimal_container"}
	parents := map[string]bool{}
	for _, module := range modules {
		parent := filepath.ToSlash(filepath.Dir(module))
		if !parents[parent] {
			pytest = append(pytest, "--cov="+parent)
			parents[parent] = true
		}
	}
	commands := [][]string{
		append(pytest, tests...),
		{"uv", "run", "coverage", "json", "--fail-under=0", "--include=" + strings.Join(modules, ","), "-o", report},
		{"uv", "run", "coverage", "xml", "--fail-under=0", "--include=" + strings.Join(modules, ","), "-o", xml},
	}
	checks := make([]execution.Check, 0, len(commands))
	for _, args := range commands {
		checks = append(checks, execution.Check{Label: "local_coverage", Directory: ".", Args: args, Env: map[string]string{"COVERAGE_FILE": data}, Timeout: 10 * time.Minute})
	}
	floorChecks, err := reportGate(floor, "coverage.json", report, modules)
	if err != nil {
		return nil, err
	}
	patchChecks, err := reportGate(patch, "coverage.xml", xml, nil)
	if err != nil {
		return nil, err
	}
	return append(append(checks, floorChecks...), patchChecks...), nil
}

// Adapt only the report path and the partial module scope. Policy, including
// patch thresholds and comparison branch, stays in the CI check groups.
func reportGate(suite execution.Suite, source, report string, modules []string) ([]execution.Check, error) {
	checks, err := execution.Commands(suite)
	if err != nil {
		return nil, err
	}
	found := false
	for i := range checks {
		for j, arg := range checks[i].Args {
			if arg == source {
				checks[i].Args[j] = report
				found = true
				for _, module := range modules {
					checks[i].Args = append(checks[i].Args, "--module", module)
				}
			}
		}
	}
	if !found {
		return nil, fmt.Errorf("coverage gate %q: expected a configured command consuming %s", suite.Name, source)
	}
	return checks, nil
}

func containsPart(parts []string, value string) bool {
	for _, part := range parts {
		if part == value {
			return true
		}
	}
	return false
}
