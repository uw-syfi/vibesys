package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func nativeFixture() graph {
	return graph{
		NativeTargets: map[string]nativeTarget{
			"engine:a": {ComponentID: "engine:a", Root: "apps/a", Language: "go", Selected: true},
			"engine:b": {ComponentID: "engine:b", Root: "apps/b", Language: "rust", Selected: true},
			"engine:c": {ComponentID: "engine:c", Root: "apps/c", Language: "go", Selected: false},
		},
		NativeChecks: nativeChecks{
			Commands: map[string][][]string{
				"go":   {{"go", "test", "./..."}},
				"rust": {{"cargo", "fmt"}, {"cargo", "test"}},
			},
			TimeoutSeconds: 1,
		},
		NativeCheckOverrides: map[string]nativeCheckOverride{},
	}
}

func TestNativeCommandsOnlySelectedRoots(t *testing.T) {
	g := nativeFixture()
	var calls []string
	run := func(_ context.Context, _ string, target string, args, _ []string) error {
		calls = append(calls, target+": "+strings.Join(args, " "))
		return nil
	}
	err := runNativeTargets(t.TempDir(), g, `["apps/a","apps/b"]`, run)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"apps/a: go test ./...", "apps/b: cargo fmt", "apps/b: cargo test"}
	if !reflect.DeepEqual(calls, want) {
		t.Fatalf("commands = %v, want %v", calls, want)
	}
}

func TestNativeEnvironmentOverridesInheritedValue(t *testing.T) {
	t.Setenv("CHECK_MODE", "old")
	g := nativeFixture()
	g.NativeCheckOverrides["apps/a"] = nativeCheckOverride{Root: "apps/a", Env: map[string]string{"CHECK_MODE": "new"}}
	var values []string
	err := runNativeTargets(t.TempDir(), g, `["apps/a"]`, func(_ context.Context, _, _ string, _ []string, env []string) error {
		for _, item := range env {
			if strings.HasPrefix(item, "CHECK_MODE=") {
				values = append(values, item)
			}
		}
		return nil
	})
	if err != nil || !reflect.DeepEqual(values, []string{"CHECK_MODE=new"}) {
		t.Fatalf("environment values = %v, err = %v", values, err)
	}
}

func TestNativeInvalidTargetsRunNothing(t *testing.T) {
	g := nativeFixture()
	for _, raw := range []string{`[]`, `not-json`, `["apps/a","apps/a"]`, `["apps/c"]`, `["../escape"]`} {
		err := runNativeTargets(t.TempDir(), g, raw, func(context.Context, string, string, []string, []string) error {
			t.Fatal("unexpected command")
			return nil
		})
		if err == nil {
			t.Fatalf("%s: expected error", raw)
		}
	}
	g.NativeTargets["bad"] = nativeTarget{Root: "../escape", Language: "go", Selected: true}
	if _, err := parseNativeTargets(`["apps/a"]`, g); err == nil || !strings.Contains(err.Error(), "unsafe") {
		t.Fatalf("unsafe metadata error = %v", err)
	}
}

func TestNativeAssertion(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "apps", "a", "go.mod")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("module example.test/a\n"), 0600); err != nil {
		t.Fatal(err)
	}
	assertion := nativeAssertion{File: "go.mod", LinePrefix: "module ", Equals: "example.test/a"}
	if err := checkNativeAssertion(root, "apps/a", assertion); err != nil {
		t.Fatal(err)
	}
	assertion.Equals = "example.test/b"
	if err := checkNativeAssertion(root, "apps/a", assertion); err == nil || !strings.Contains(err.Error(), "expected") {
		t.Fatalf("assertion error = %v", err)
	}
}

func TestNativeCommandFailures(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "apps", "a"), 0700); err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name    string
		command []string
		want    string
	}{
		{"missing executable", []string{"repoctl-command-that-does-not-exist"}, "executable file not found"},
		{"nonzero exit", []string{"sh", "-c", "exit 7"}, "exit status 7"},
		{"timeout", []string{"sh", "-c", "exec sleep 5"}, "timed out"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			g := nativeFixture()
			g.NativeChecks.Commands["go"] = [][]string{tt.command}
			err := runNativeTargets(root, g, `["apps/a"]`, runNativeCommand)
			if err == nil || !strings.Contains(err.Error(), tt.want) {
				t.Fatalf("error = %v, want %q", err, tt.want)
			}
		})
	}
}

func TestNativeRunnerStopsOnFirstFailure(t *testing.T) {
	g := nativeFixture()
	count := 0
	err := runNativeTargets(t.TempDir(), g, `["apps/b"]`, func(context.Context, string, string, []string, []string) error {
		count++
		return errors.New("failed")
	})
	if err == nil || count != 1 {
		t.Fatalf("error = %v, commands run = %d", err, count)
	}
}
