package main

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"
	"time"
)

func run(root, program string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, program, args...)
	cmd.Dir = root
	out, err := cmd.Output()
	if err != nil {
		if e, ok := err.(*exec.ExitError); ok {
			return nil, fmt.Errorf("%s %s failed: %s", program, strings.Join(args, " "), strings.TrimSpace(string(e.Stderr)))
		}
		return nil, err
	}
	return out, nil
}
func changedPaths(root, base, head, event string) ([]string, error) {
	if base == "" || head == "" {
		return nil, fmt.Errorf("both --base and --head are required")
	}
	zero := strings.Trim(base, "0") == ""
	if zero && event != "push" {
		return nil, fmt.Errorf("zero base SHA is only valid for an initial push")
	}
	if event == "pull_request" {
		out, err := run(root, "git", "merge-base", base, head)
		if err != nil {
			return nil, err
		}
		base = strings.TrimSpace(string(out))
	} else if event != "merge_group" && event != "push" {
		return nil, fmt.Errorf("unsupported event %q", event)
	}
	args := []string{"diff", "--name-status", "-z", "--find-renames", base, head}
	if zero {
		args = []string{"diff-tree", "--root", "--no-commit-id", "--name-status", "-z", "-r", head}
	}
	out, err := run(root, "git", args...)
	if err != nil {
		return nil, err
	}
	parts := bytes.Split(out, []byte{0})
	paths := []string{}
	seen := map[string]bool{}
	for i := 0; i < len(parts)-1; {
		status := string(parts[i])
		count := 1
		if strings.HasPrefix(status, "R") || strings.HasPrefix(status, "C") {
			count = 2
		}
		if status == "" || !strings.ContainsRune("ACDMRTUXB", rune(status[0])) || i+count >= len(parts) {
			return nil, fmt.Errorf("malformed git diff status %q", status)
		}
		for j := 1; j <= count; j++ {
			p := string(parts[i+j])
			if !seen[p] {
				paths = append(paths, p)
				seen[p] = true
			}
		}
		i += count + 1
	}
	return paths, nil
}
func trackedPaths(root string) ([]string, error) {
	out, err := run(root, "git", "ls-files", "-z")
	if err != nil {
		return nil, err
	}
	paths := []string{}
	for _, p := range bytes.Split(out, []byte{0}) {
		if len(p) > 0 {
			paths = append(paths, string(p))
		}
	}
	return paths, nil
}
