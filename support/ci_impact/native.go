package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

var envKey = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

type nativeCommand func(ctx context.Context, root, target string, args, env []string) error

func runNativeCommand(ctx context.Context, root, target string, args, env []string) error {
	cmd := exec.CommandContext(ctx, args[0], args[1:]...)
	cmd.Dir = filepath.Join(root, filepath.FromSlash(target))
	cmd.Env = env
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	err := cmd.Run()
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return err
}

func selectedNativeTargets(g graph) (map[string]nativeTarget, error) {
	selected := map[string]nativeTarget{}
	for _, target := range g.NativeTargets {
		if !target.Selected {
			continue
		}
		if !validPath(target.Root) {
			return nil, fmt.Errorf("unsafe native target root %q", target.Root)
		}
		if _, exists := selected[target.Root]; exists {
			return nil, fmt.Errorf("duplicate selected native target root %q", target.Root)
		}
		selected[target.Root] = target
	}
	return selected, nil
}

func parseNativeTargets(raw string, g graph) ([]string, error) {
	var targets []string
	if err := json.Unmarshal([]byte(raw), &targets); err != nil {
		return nil, fmt.Errorf("native targets must be a JSON array: %w", err)
	}
	if len(targets) == 0 {
		return nil, fmt.Errorf("native targets must be a nonempty JSON array of paths")
	}
	registered, err := selectedNativeTargets(g)
	if err != nil {
		return nil, err
	}
	seen := map[string]bool{}
	for _, root := range targets {
		if seen[root] {
			return nil, fmt.Errorf("duplicate native target %q", root)
		}
		seen[root] = true
		target, ok := registered[root]
		if !ok {
			return nil, fmt.Errorf("unregistered native target %q", root)
		}
		if len(g.NativeChecks.Commands[target.Language]) == 0 {
			return nil, fmt.Errorf("native target %q has no commands for language %q", root, target.Language)
		}
	}
	return targets, nil
}

func checkNativeAssertion(root, target string, assertion nativeAssertion) error {
	if !validPath(target) || !validPath(assertion.File) {
		return fmt.Errorf("unsafe native assertion path for %q", target)
	}
	path := filepath.Join(root, filepath.FromSlash(target), filepath.FromSlash(assertion.File))
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	found := false
	for _, line := range strings.Split(string(raw), "\n") {
		if !strings.HasPrefix(line, assertion.LinePrefix) {
			continue
		}
		if found {
			return fmt.Errorf("%s: duplicate line prefix %q", path, assertion.LinePrefix)
		}
		found = true
		declared := strings.TrimSpace(strings.TrimPrefix(line, assertion.LinePrefix))
		if declared != assertion.Equals {
			return fmt.Errorf("%s: declares %q; expected %q", path, declared, assertion.Equals)
		}
	}
	if !found {
		return fmt.Errorf("%s: missing line prefix %q", path, assertion.LinePrefix)
	}
	return nil
}

func nativeEnvironment(base []string, overrides map[string]string) []string {
	if len(overrides) == 0 {
		return base
	}
	env := make([]string, 0, len(base)+len(overrides))
	for _, entry := range base {
		key, _, _ := strings.Cut(entry, "=")
		if _, replaced := overrides[key]; replaced {
			continue
		}
		env = append(env, entry)
	}
	keys := make([]string, 0, len(overrides))
	for key := range overrides {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		env = append(env, key+"="+overrides[key])
	}
	return env
}

func runNativeTargets(root string, g graph, raw string, run nativeCommand) error {
	targets, err := parseNativeTargets(raw, g)
	if err != nil {
		return err
	}
	if g.NativeChecks.TimeoutSeconds <= 0 {
		return fmt.Errorf("native_checks.timeout_seconds must be positive")
	}
	registered, err := selectedNativeTargets(g)
	if err != nil {
		return err
	}
	for _, targetRoot := range targets {
		target := registered[targetRoot]
		override := g.NativeCheckOverrides[targetRoot]
		for _, assertion := range override.Assertions {
			if err := checkNativeAssertion(root, targetRoot, assertion); err != nil {
				return err
			}
		}
		env := nativeEnvironment(os.Environ(), override.Env)
		for _, args := range g.NativeChecks.Commands[target.Language] {
			if len(args) == 0 || args[0] == "" {
				return fmt.Errorf("native target %q has an empty command", targetRoot)
			}
			fmt.Printf("%s: %s\n", targetRoot, strings.Join(args, " "))
			ctx, cancel := context.WithTimeout(context.Background(), time.Duration(g.NativeChecks.TimeoutSeconds)*time.Second)
			err := run(ctx, root, targetRoot, args, env)
			cancel()
			if err != nil {
				if errors.Is(err, context.DeadlineExceeded) || ctx.Err() == context.DeadlineExceeded {
					return fmt.Errorf("%s: %s: timed out after %d seconds", targetRoot, strings.Join(args, " "), g.NativeChecks.TimeoutSeconds)
				}
				return fmt.Errorf("%s: %s: %w", targetRoot, strings.Join(args, " "), err)
			}
		}
	}
	return nil
}
