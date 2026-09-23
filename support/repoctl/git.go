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

// parseNameStatusZ parses `git diff --name-status -z` output. In NUL mode Git
// separates each status and pathname with NUL, including both paths for a
// rename or copy. Pathnames are otherwise opaque bytes represented as strings.
func parseNameStatusZ(raw []byte) ([]string, error) {
	if len(raw) == 0 {
		return []string{}, nil
	}
	if raw[len(raw)-1] != 0 {
		return nil, fmt.Errorf("malformed git diff output: missing final NUL")
	}

	fields := bytes.Split(raw[:len(raw)-1], []byte{0})
	paths := make([]string, 0, len(fields))
	seen := make(map[string]bool, len(fields))
	for i := 0; i < len(fields); {
		status := string(fields[i])
		count, ok := nameStatusPathCount(status)
		if !ok {
			return nil, fmt.Errorf("malformed git diff status %q", status)
		}
		if len(fields)-i-1 < count {
			return nil, fmt.Errorf("truncated git diff record for status %q", status)
		}
		for _, field := range fields[i+1 : i+1+count] {
			if len(field) == 0 {
				return nil, fmt.Errorf("empty pathname for status %q", status)
			}
			path := string(field)
			if !seen[path] {
				paths = append(paths, path)
				seen[path] = true
			}
		}
		i += count + 1
	}
	return paths, nil
}

func nameStatusPathCount(status string) (int, bool) {
	switch status {
	case "A", "M", "D", "T", "U", "X", "B":
		return 1, true
	}
	if len(status) < 2 || (status[0] != 'R' && status[0] != 'C') || len(status) > 4 {
		return 0, false
	}
	score := 0
	for _, digit := range status[1:] {
		if digit < '0' || digit > '9' {
			return 0, false
		}
		score = score*10 + int(digit-'0')
	}
	return 2, score <= 100
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
	return parseNameStatusZ(out)
}

// worktreePaths includes staged and unstaged tracked changes and untracked files.
// Both sides of a rename are retained so ownership at either path is selected.
func worktreePaths(root string) ([]string, error) {
	tracked, err := run(root, "git", "diff", "--name-status", "-z", "--find-renames", "HEAD")
	if err != nil {
		return nil, err
	}
	paths, err := parseNameStatusZ(tracked)
	if err != nil {
		return nil, err
	}
	untracked, err := run(root, "git", "ls-files", "--others", "--exclude-standard", "-z")
	if err != nil {
		return nil, err
	}
	seen := make(map[string]bool, len(paths))
	for _, path := range paths {
		seen[path] = true
	}
	for _, raw := range bytes.Split(untracked, []byte{0}) {
		if len(raw) == 0 {
			continue
		}
		path := string(raw)
		if !seen[path] {
			paths = append(paths, path)
			seen[path] = true
		}
	}
	return paths, nil
}

func appendUniquePaths(paths, added []string) []string {
	seen := make(map[string]bool, len(paths))
	for _, path := range paths {
		seen[path] = true
	}
	for _, path := range added {
		if !seen[path] {
			paths = append(paths, path)
			seen[path] = true
		}
	}
	return paths
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
