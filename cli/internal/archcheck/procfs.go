package archcheck

import (
	"fmt"
	"go/ast"
	"go/build/constraint"
	"go/parser"
	"go/token"
	"path"
	"regexp"
	"slices"
	"strconv"
	"strings"
)

// /proc is the Linux process file system, and macOS has none. A test that reads it for the processes
// it started finds none there: a helper that took "cannot read" for "nothing to find" passed on
// macOS while it left the daemons it was to end running (the sweep of a test machine did, and the
// daemons it never found stayed on the machine). The processes of a machine are read through internal/proctable,
// which reads the table the way each system gives it and fails when it cannot. A test file that names
// a /proc path in a string is therefore Linux-only, by a _linux name or a //go:build constraint that
// keeps it off macOS, so that it does not build and run there to find nothing, or it is listed with
// the reason it runs where /proc is missing.
//
// The rule holds test files: a script a product file sends to a Linux node over ssh names /proc and
// runs there, and a production reader of /proc is a platform decision of its own.

// procPath matches /proc as a path, at the start of a word or of a string, not inside a longer name.
var procPath = regexp.MustCompile(`(^|[^[:alnum:]_./-])/proc($|[^[:alnum:]_-])`)

// procExceptions lists the test files that name /proc and are built on macOS.
var procExceptions = []Exception{
	{"cli/internal/environment/slurm/slurm_test.go",
		"runAllocated reads the CPUs this process may run on from /proc/self/status, which only Linux has, and fails the test where it is missing, because sed exits non-zero"},
}

// ProcExceptions returns the test files excused from the /proc rule.
func ProcExceptions() []Exception { return append([]Exception(nil), procExceptions...) }

// procReads reports each string of a test file that names a /proc path, unless the file is built
// on Linux only.
func procReads(name, source string, honorExceptions bool) []string {
	if !strings.HasSuffix(name, "_test.go") || !strings.HasPrefix(name, "cli/") ||
		strings.HasPrefix(name, "cli/internal/archcheck/") || strings.HasPrefix(name, genAt) ||
		(honorExceptions && excused(procExceptions, name)) || linuxOnly(name, source) {
		return nil
	}
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, name, source, 0)
	if err != nil {
		return nil // layering reports the unparsable file
	}
	var found []string
	ast.Inspect(file, func(n ast.Node) bool {
		lit, ok := n.(*ast.BasicLit)
		if !ok || lit.Kind != token.STRING {
			return true
		}
		text, err := strconv.Unquote(lit.Value)
		if err != nil {
			text = lit.Value
		}
		if procPath.MatchString(text) {
			found = append(found, fmt.Sprintf(
				"%s:%d: names a path under /proc, which macOS does not have, so on macOS it reads nothing; read processes through internal/proctable, "+
					"or make the file Linux-only (a _linux name or //go:build linux), or list it in procExceptions with the reason it runs where /proc is missing",
				name, fset.Position(lit.Pos()).Line))
		}
		return true
	})
	return found
}

// goArchs are the architectures a file name may end with, after the system, to restrict the file to
// that pair, as in x_linux_amd64_test.go.
var goArchs = []string{"386", "amd64", "arm", "arm64", "loong64", "mips", "mipsle", "mips64", "mips64le", "ppc64", "ppc64le", "riscv64", "s390x", "wasm"}

// linuxOnly says whether the file is built on Linux and not on macOS: it is named for Linux, or its
// build constraint is false on macOS however the other tags are set, which a constraint such as
// "race" or "docker", that does not name the system, is not.
func linuxOnly(name, source string) bool {
	parts := strings.Split(strings.TrimSuffix(strings.TrimSuffix(path.Base(name), ".go"), "_test"), "_")
	if n := len(parts); n > 1 && (parts[n-1] == "linux" || n > 2 && parts[n-2] == "linux" && slices.Contains(goArchs, parts[n-1])) {
		return true
	}
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
		return excludesMacOS(expr)
	}
	return false
}

// excludesMacOS says whether the constraint is false on macOS however its other tags (race, docker,
// an architecture) are set.
func excludesMacOS(expr constraint.Expr) bool {
	var free []string
	var collect func(constraint.Expr)
	collect = func(e constraint.Expr) {
		switch e := e.(type) {
		case *constraint.TagExpr:
			if !slices.Contains([]string{"darwin", "unix", "linux"}, e.Tag) && !slices.Contains(free, e.Tag) {
				free = append(free, e.Tag)
			}
		case *constraint.NotExpr:
			collect(e.X)
		case *constraint.AndExpr:
			collect(e.X)
			collect(e.Y)
		case *constraint.OrExpr:
			collect(e.X)
			collect(e.Y)
		}
	}
	collect(expr)
	if len(free) > 12 {
		return false // a constraint this tangled is not read as excluding macOS
	}
	for set := 0; set < 1<<len(free); set++ {
		builtOnMac := expr.Eval(func(tag string) bool {
			switch tag {
			case "darwin", "unix":
				return true
			case "linux":
				return false
			}
			return set&(1<<slices.Index(free, tag)) != 0
		})
		if builtOnMac {
			return false
		}
	}
	return true
}
