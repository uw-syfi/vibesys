package docker

import (
	"reflect"
	"strings"
	"testing"

	"repoctl/execution"
)

func buildSuite() execution.Suite {
	return execution.Suite{
		Name: "builds", Language: "docker", Directory: ".", Collection: "contexts", TimeoutSeconds: 5,
		Commands:        [][]string{{"docker", "info"}},
		PackageCommands: [][]string{{"docker", "build", "{package}"}},
	}
}

func TestEachSelectedContextBuildsOnceInSortedOrder(t *testing.T) {
	checks, err := Planner{}.Plan(buildSuite(), []string{"b/ctx", "a/ctx", "c/ctx"})
	if err != nil {
		t.Fatal(err)
	}
	var got [][]string
	for _, check := range checks {
		got = append(got, check.Args)
	}
	want := [][]string{{"docker", "build", "a/ctx"}, {"docker", "build", "b/ctx"}, {"docker", "build", "c/ctx"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("commands = %v, want %v", got, want)
	}
}

func TestNoSelectedContextRunsTheFallbackCommands(t *testing.T) {
	checks, err := Planner{}.Plan(buildSuite(), nil)
	if err != nil || len(checks) != 1 || strings.Join(checks[0].Args, " ") != "docker info" {
		t.Fatalf("checks = %v, err = %v", checks, err)
	}
}

func TestInvalidSuitesAreRejectedAtValidation(t *testing.T) {
	known := map[string]bool{"contexts": true}
	noPlaceholder := buildSuite()
	noPlaceholder.PackageCommands = [][]string{{"docker", "build", "."}}
	unknown := buildSuite()
	unknown.Collection = "elsewhere"
	orphan := buildSuite()
	orphan.Collection = ""
	for name, suite := range map[string]execution.Suite{"no placeholder": noPlaceholder, "unknown collection": unknown, "commands without collection": orphan} {
		if err := (Planner{}).Validate(suite, known); err == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
	if err := (Planner{}).Validate(buildSuite(), known); err != nil {
		t.Fatal(err)
	}
}
