package archcheck

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"slices"
	"strconv"
	"strings"
)

// The daemon's flows are simulated with a seeded clock and a scheduler of their own, so two things
// that would escape the simulation are refused in the source: reading or waiting on the real clock,
// and starting a goroutine nobody owns.

// clockEntryPoints are the functions of package time that read the real clock or wait on it. Pure
// constructors and conversions such as time.Date, time.Unix and time.FixedZone are allowed.
var clockEntryPoints = []string{
	"Now", "Since", "Until", "Sleep", "After", "AfterFunc", "Tick", "NewTimer", "NewTicker",
}

// clockAt is the one package that may name them: it is the seam the rest of the code is given.
const clockAt = "cli/internal/clock/"

// goStatementScopes are the package trees whose non-test files may not contain a go statement.
// Their goroutines start through owned.Group, which joins them. Test files are not held to it: a
// test starts its own client goroutines and the testing skill's concurrency kit bounds them.
var goStatementScopes = []string{"cli/internal/app/", "cli/internal/daemon/", "cli/internal/supervisor/"}

// ownedAt is the package that implements the owned goroutine group; it is the one place that is
// allowed to be written in terms of goroutine primitives.
const ownedAt = "cli/internal/owned/"

// Exception is a file that is allowed to break a rule, and why. Each one is listed so that the
// reason is read in review, and a stale one fails the check.
type Exception struct {
	// File is the slash-separated path from the repository root.
	File string
	// Reason says why the rule does not apply, or what will remove the exception.
	Reason string
}

// clockExceptions lists the files outside internal/clock that name a clock entry point.
var clockExceptions = []Exception{
	{"cli/internal/store/storetest/concurrency.go",
		"a hang guard: time.After bounds how long the suite waits for a store call that may never return, and no result depends on it"},
	{"cli/internal/store/ledger/hold_test.go",
		"TEMPORARY: reads time.Now() only to have an arbitrary instant with a monotonic reading to hold; replace with a literal instant"},
}

// goExceptions lists the files in goStatementScopes that start a goroutine without an owned.Group.
var goExceptions = []Exception{
	{"cli/internal/supervisor/host/host.go",
		"TEMPORARY: reaps the supervisor child with cmd.Wait; a later daemon is not its parent, so no owner can join it; move behind owned.Group"},
	{"cli/internal/supervisor/suptest/suptest.go",
		"TEMPORARY: the test helper follows a supervisor in a goroutine it joins through a channel; move behind owned.Group"},
	{"cli/internal/daemon/daemontest/daemontest.go",
		"TEMPORARY: the test daemon runs daemon.Run in a goroutine it joins through a channel; move behind owned.Group"},
}

// Exceptions returns the files excused from the clock rule and from the go-statement rule.
func Exceptions() (clock, goStatements []Exception) {
	return slices.Clone(clockExceptions), slices.Clone(goExceptions)
}

func excused(list []Exception, name string) bool {
	return slices.ContainsFunc(list, func(e Exception) bool { return e.File == name })
}

// effects reports a use of the real clock and, in goStatementScopes, a bare go statement.
func effects(name, source string) []string {
	return effectsFor(name, source, true)
}

func effectsFor(name, source string, honorExceptions bool) []string {
	return append(clockUses(name, source, honorExceptions), goStatements(name, source, honorExceptions)...)
}

func parseFull(name, source string) (*ast.File, error) {
	file, err := parser.ParseFile(token.NewFileSet(), name, source, 0)
	if err != nil {
		return nil, fmt.Errorf("%s: cannot parse: %w", name, err)
	}
	return file, nil
}

func clockUses(name, source string, honorExceptions bool) []string {
	if strings.HasPrefix(name, clockAt) || strings.HasPrefix(name, "cli/gen/") ||
		(honorExceptions && excused(clockExceptions, name)) {
		return nil
	}
	if !strings.Contains(source, `"time"`) {
		return nil
	}
	file, err := parseFull(name, source)
	if err != nil {
		return nil // layering reports the unparsable file
	}
	local := ""
	for _, spec := range file.Imports {
		if target, _ := strconv.Unquote(spec.Path.Value); target == "time" {
			local = "time"
			if spec.Name != nil {
				local = spec.Name.Name
			}
		}
	}
	if local == "" || local == "_" {
		return nil
	}
	var found []string
	ast.Inspect(file, func(n ast.Node) bool {
		sel, ok := n.(*ast.SelectorExpr)
		if !ok {
			return true
		}
		if id, ok := sel.X.(*ast.Ident); ok && id.Name == local && id.Obj == nil && slices.Contains(clockEntryPoints, sel.Sel.Name) {
			found = append(found, fmt.Sprintf("%s: uses time.%s; the real clock is reached only through internal/clock", name, sel.Sel.Name))
		}
		return true
	})
	return found
}

func goStatements(name, source string, honorExceptions bool) []string {
	inScope := slices.ContainsFunc(goStatementScopes, func(p string) bool { return strings.HasPrefix(name, p) })
	if !inScope || strings.HasSuffix(name, "_test.go") || strings.HasPrefix(name, ownedAt) ||
		(honorExceptions && excused(goExceptions, name)) {
		return nil
	}
	file, err := parseFull(name, source)
	if err != nil {
		return nil
	}
	var found []string
	ast.Inspect(file, func(n ast.Node) bool {
		if _, ok := n.(*ast.GoStmt); ok {
			found = append(found, fmt.Sprintf("%s: starts a goroutine with a bare go statement; use owned.Group", name))
		}
		return true
	})
	return found
}

// StaleExceptions returns one line for each listed exception whose file, in files, no longer breaks
// the rule it is excused from, or no longer exists, so a fixed violation loses its exception.
func StaleExceptions(files map[string]string) []string {
	var stale []string
	check := func(rule string, list []Exception, uses func(name, source string, honor bool) []string) {
		for _, e := range list {
			source, ok := files[e.File]
			if !ok {
				stale = append(stale, fmt.Sprintf("%s: the %s exception names a file that does not exist", e.File, rule))
			} else if len(uses(e.File, source, false)) == 0 {
				stale = append(stale, fmt.Sprintf("%s: the %s exception is no longer needed; remove it", e.File, rule))
			}
		}
	}
	check("clock", clockExceptions, clockUses)
	check("go statement", goExceptions, goStatements)
	slices.Sort(stale)
	return stale
}
