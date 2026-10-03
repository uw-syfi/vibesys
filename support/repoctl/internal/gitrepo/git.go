// Package gitrepo reads repository revisions and working-tree changes for
// repoctl. It keeps Git status parsing and snapshot management out of planning.
package gitrepo

import (
	"bytes"
	"fmt"
	"strings"

	"repoctl/internal/command"
)

// parseNameStatusZ parses `git diff --name-status -z` output. In NUL mode Git
// separates each status and pathname with NUL, including both paths for a
// rename or copy. Pathnames are otherwise opaque bytes represented as strings.
func parseNameStatusZ(raw []byte) ([]string, error) {
	records, err := parseNameStatusRecordsZ(raw)
	if err != nil {
		return nil, err
	}
	paths := make([]string, 0, len(records)*2)
	seen := map[string]bool{}
	for _, record := range records {
		for _, path := range record.paths() {
			if !seen[path] {
				paths = append(paths, path)
				seen[path] = true
			}
		}
	}
	return paths, nil
}

// Change is one Git path-status record. Path holds a single-path change;
// OldPath and NewPath are set for copies and renames.
type Change struct {
	Status  string
	Path    string
	OldPath string
	NewPath string
}

func (c Change) paths() []string {
	if c.Status[0] == 'R' || c.Status[0] == 'C' {
		return []string{c.OldPath, c.NewPath}
	}
	return []string{c.Path}
}

func parseNameStatusRecordsZ(raw []byte) ([]Change, error) {
	if len(raw) == 0 {
		return []Change{}, nil
	}
	if raw[len(raw)-1] != 0 {
		return nil, fmt.Errorf("malformed git diff output: missing final NUL")
	}

	fields := bytes.Split(raw[:len(raw)-1], []byte{0})
	records := make([]Change, 0, len(fields)/2)
	for i := 0; i < len(fields); {
		status := string(fields[i])
		count, ok := nameStatusPathCount(status)
		if !ok {
			return nil, fmt.Errorf("malformed git diff status %q", status)
		}
		if len(fields)-i-1 < count {
			return nil, fmt.Errorf("truncated git diff record for status %q", status)
		}
		paths := make([]string, count)
		for j, field := range fields[i+1 : i+1+count] {
			if len(field) == 0 {
				return nil, fmt.Errorf("empty pathname for status %q", status)
			}
			paths[j] = string(field)
		}
		record := Change{Status: status, Path: paths[0]}
		if count == 2 {
			record.OldPath, record.NewPath = paths[0], paths[1]
		}
		records = append(records, record)
		i += count + 1
	}
	return records, nil
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

// Comparison contains the effective base and changes to head.
type Comparison struct {
	BaseRevision string
	Changes      []Change
}

// Compare computes changes between revisions using repoctl event semantics.
func Compare(root, base, head, event string) (Comparison, error) {
	zero := strings.Trim(base, "0") == ""
	base, err := comparisonBase(root, base, head, event)
	if err != nil {
		return Comparison{}, err
	}
	args := []string{"diff", "--name-status", "-z", "--find-renames", base, head}
	if zero {
		args = []string{"diff-tree", "--root", "--no-commit-id", "--name-status", "-z", "-r", head}
	}
	out, err := command.Run(root, "git", args...)
	if err != nil {
		return Comparison{}, err
	}
	changes, err := parseNameStatusRecordsZ(out)
	if err != nil {
		return Comparison{}, err
	}
	return Comparison{BaseRevision: base, Changes: changes}, nil
}

// ChangedPaths returns unique paths from a revision comparison.
func ChangedPaths(root, base, head, event string) ([]string, error) {
	comparison, err := Compare(root, base, head, event)
	if err != nil {
		return nil, err
	}
	paths := []string{}
	for _, change := range comparison.Changes {
		paths = append(paths, change.paths()...)
	}
	return uniquePaths(nil, paths), nil
}

func comparisonBase(root, base, head, event string) (string, error) {
	if base == "" || head == "" {
		return "", fmt.Errorf("both --base and --head are required")
	}
	zero := strings.Trim(base, "0") == ""
	if zero && event != "push" {
		return "", fmt.Errorf("zero base SHA is only valid for an initial push")
	}
	if event == "pull_request" {
		out, err := command.Run(root, "git", "merge-base", base, head)
		if err != nil {
			return "", err
		}
		return strings.TrimSpace(string(out)), nil
	}
	if event != "merge_group" && event != "push" {
		return "", fmt.Errorf("unsupported event %q", event)
	}
	return base, nil
}

// WorktreePaths includes staged and unstaged tracked changes and untracked files.
// Both sides of a rename are retained so ownership at either path is selected.
func WorktreePaths(root string) ([]string, error) {
	changes, err := WorktreeChanges(root)
	if err != nil {
		return nil, err
	}
	paths := []string{}
	for _, change := range changes {
		paths = append(paths, change.paths()...)
	}
	return uniquePaths(nil, paths), nil
}

// WorktreeChanges returns staged, unstaged, and untracked changes since HEAD.
func WorktreeChanges(root string) ([]Change, error) {
	tracked, err := command.Run(root, "git", "diff", "--name-status", "-z", "--find-renames", "HEAD")
	if err != nil {
		return nil, err
	}
	changes, err := parseNameStatusRecordsZ(tracked)
	if err != nil {
		return nil, err
	}
	untracked, err := command.Run(root, "git", "ls-files", "--others", "--exclude-standard", "-z")
	if err != nil {
		return nil, err
	}
	seen := make(map[string]bool)
	for _, change := range changes {
		for _, path := range change.paths() {
			seen[path] = true
		}
	}
	for _, raw := range bytes.Split(untracked, []byte{0}) {
		if len(raw) == 0 {
			continue
		}
		path := string(raw)
		if !seen[path] {
			changes = append(changes, Change{Status: "A", Path: path})
			seen[path] = true
		}
	}
	return changes, nil
}

func uniquePaths(paths, added []string) []string {
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

// TrackedPaths returns repository paths known to the Git index.
func TrackedPaths(root string) ([]string, error) {
	out, err := command.Run(root, "git", "ls-files", "-z")
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
