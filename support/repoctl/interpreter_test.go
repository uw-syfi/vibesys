package main

import (
	"context"
	"strings"
	"testing"
	"testing/quick"

	"repoctl/execution"
	"repoctl/internal/doctor"
)

// fakeInterpreters is a machine with python3 executables at given paths and versions.
type fakeInterpreters struct {
	versions map[string]string
	path     []string
}

func (f fakeInterpreters) Exists(path string) bool { _, ok := f.versions[path]; return ok }
func (f fakeInterpreters) Version(path string) string {
	return f.versions[path]
}
func (f fakeInterpreters) PathDirectories() []string { return f.path }

const repo = "/work/repo"

func TestAnInterpreterThatMeetsTheMinimumIsChosenWhateverPathHoldsFirst(t *testing.T) {
	versions := []string{"3.8.10", "3.11.2", "3.12.0", "3.12.3", "3.13.1"}
	err := quick.Check(func(venv, first, second uint8) bool {
		host := fakeInterpreters{
			versions: map[string]string{
				"/usr/bin/python3":    versions[int(first)%len(versions)],
				"/opt/py/bin/python3": versions[int(second)%len(versions)],
			},
			path: []string{"/usr/bin", "/opt/py/bin"},
		}
		if venv%2 == 0 {
			host.versions[repo+"/.venv/bin/python3"] = versions[int(venv)%len(versions)]
		}
		got, err := repositoryInterpreter(repo, "3.12", "fix it", host)
		if err != nil {
			// Only when no copy anywhere meets the minimum.
			for _, v := range host.versions {
				if doctor.CompareVersions(v, "3.12") >= 0 {
					return false
				}
			}
			return strings.Contains(err.Error(), "fix it") && strings.Contains(err.Error(), "3.12")
		}
		return doctor.CompareVersions(host.versions[got], "3.12") >= 0
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
}

func TestTheRepositoryEnvironmentComesBeforePath(t *testing.T) {
	host := fakeInterpreters{
		versions: map[string]string{repo + "/.venv/bin/python3": "3.12.1", "/usr/bin/python3": "3.13.0"},
		path:     []string{"/usr/bin"},
	}
	got, err := repositoryInterpreter(repo, "3.12", "", host)
	if err != nil || got != repo+"/.venv/bin/python3" {
		t.Fatalf("got %q, %v", got, err)
	}
}

func TestASystemPythonThatIsTooOldFailsAtOnceNamingWhatIsOnTheMachine(t *testing.T) {
	host := fakeInterpreters{versions: map[string]string{"/usr/bin/python3": "3.11.2"}, path: []string{"/usr/bin"}}
	_, err := repositoryInterpreter(repo, "3.12", "Run doctor --fix.", host)
	if err == nil {
		t.Fatal("accepted Python 3.11 for a repository that needs 3.12")
	}
	for _, want := range []string{"3.12", "/usr/bin/python3 (3.11.2)", "Run doctor --fix."} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error %q lacks %q", err, want)
		}
	}
}

func TestOnlyBarePythonChecksAreRewrittenAndAnUnusableMachineStopsThePlanBeforeAnyRuns(t *testing.T) {
	inner := &recordingRunner{}
	runner := &pythonRunner{
		inner: inner, root: repo, minimum: "3.12", remedy: "fix",
		host: fakeInterpreters{versions: map[string]string{"/usr/bin/python3": "3.12.4"}, path: []string{"/usr/bin"}},
	}
	checks := []execution.Check{
		{Label: "always_on", Args: []string{"python3", "-m", "scripts.check_skills"}},
		{Label: "python_tests", Args: []string{"uv", "run", "python3"}},
	}
	for _, c := range checks {
		if err := runner.Run(context.Background(), repo, c); err != nil {
			t.Fatal(err)
		}
	}
	if inner.checks[0].Args[0] != "/usr/bin/python3" || inner.checks[0].Args[1] != "-m" {
		t.Fatalf("bare python3 was not resolved: %v", inner.checks[0].Args)
	}
	if inner.checks[1].Args[0] != "uv" {
		t.Fatalf("a check that is not a bare python3 was changed: %v", inner.checks[1].Args)
	}

	old := &pythonRunner{
		inner: inner, root: repo, minimum: "3.12", remedy: "fix",
		host: fakeInterpreters{versions: map[string]string{"/usr/bin/python3": "3.11.2"}, path: []string{"/usr/bin"}},
	}
	if err := old.Preflight(checks); err == nil || !strings.Contains(err.Error(), "always_on") {
		t.Fatalf("preflight = %v", err)
	}
	before := len(inner.checks)
	if err := runChecks(repo, checks, false, old); err == nil || len(inner.checks) != before {
		t.Fatalf("runChecks ran %d checks before refusing: %v", len(inner.checks)-before, err)
	}
}
