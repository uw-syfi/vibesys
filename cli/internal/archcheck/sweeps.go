package archcheck

import (
	"fmt"
	"go/ast"
	"go/build/constraint"
	"slices"
	"strconv"
	"strings"
)

// The race detector earns its cost where goroutines interleave. A sweep is a test that walks a
// large space of cases on one goroutine, such as the model check of the lifecycle machine or a
// crash at every step of a store's script: under the detector it runs ten times slower and has
// nothing to check. check.sh runs every package's tests under the detector except the sweeps,
// which are not built then, and runs every test, sweeps included, in a second step without it.
// The convention that selects them is held here:
//
//   - a test file that is not built under -race is named *_sweep_test.go, and the reverse;
//   - a sweep starts no goroutine and takes no lock, so the detector has nothing to find in it.

const sweepSuffix = "_sweep_test.go"

// sweepViolations checks one file against the sweep convention.
func sweepViolations(name, source string) []string {
	if !strings.HasSuffix(name, "_test.go") {
		return nil
	}
	sweep := strings.HasSuffix(name, sweepSuffix)
	excluded := excludedUnderRace(source)
	var found []string
	switch {
	case excluded && !sweep:
		found = append(found, fmt.Sprintf("%s: is not built under -race, so it is a sweep; name it *%s, and keep goroutines out of it", name, sweepSuffix))
	case sweep && !excluded:
		found = append(found, fmt.Sprintf("%s: is a sweep and must carry //go:build !race, so check.sh runs it without the race detector", name))
	}
	if sweep {
		found = append(found, sweepConcurrency(name, source)...)
	}
	return found
}

// excludedUnderRace says whether the file's build constraint keeps it out of a build with the race
// detector and in a build without it.
func excludedUnderRace(source string) bool {
	for _, line := range strings.Split(source, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "package ") {
			return false
		}
		if !constraint.IsGoBuild(line) {
			continue
		}
		expr, err := constraint.Parse(line)
		if err != nil {
			return false
		}
		// With every other tag set, as in a build that uses them all, does the file depend on race?
		underRace := expr.Eval(func(string) bool { return true })
		without := expr.Eval(func(tag string) bool { return tag != "race" })
		return !underRace && without
	}
	return false
}

// sweepConcurrency reports a go statement, and an import of a package for locks, in a sweep.
func sweepConcurrency(name, source string) []string {
	file, err := parseFull(name, source)
	if err != nil {
		return nil // layering reports the unparsable file
	}
	var found []string
	for _, spec := range file.Imports {
		if target, _ := strconv.Unquote(spec.Path.Value); slices.Contains([]string{"sync", "sync/atomic"}, target) {
			found = append(found, fmt.Sprintf("%s: a sweep imports %s; code that shares memory between goroutines belongs in a test the race detector runs", name, target))
		}
	}
	ast.Inspect(file, func(n ast.Node) bool {
		if _, ok := n.(*ast.GoStmt); ok {
			found = append(found, fmt.Sprintf("%s: a sweep starts a goroutine; a test with goroutines belongs where the race detector runs it", name))
		}
		return true
	})
	return found
}
