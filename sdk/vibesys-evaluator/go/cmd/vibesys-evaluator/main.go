// Command vibesys-evaluator is the core CLI the language SDKs wrap: one subcommand per coarse
// operation, called as `vibesys-evaluator <group> <verb>`.
//
// The calling convention (docs/spec.md, "Wrapper calling convention"):
//   - the request is one protobuf-JSON message on stdin, the response one protobuf-JSON message
//     on stdout;
//   - progress and logs go to stderr;
//   - exit 0 is OK, exit 1 is a domain error (the response carries error_kind and message), any
//     other code means the core itself broke;
//   - a long-running verb streams newline-delimited JSON events on stdout and ends with the
//     response.
//
// Each group registers its verbs in its own file, from an init function calling register, so
// groups never touch each other's files.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
)

// Exit codes of the convention.
const (
	exitOK     = 0
	exitDomain = 1
	exitCore   = 2
)

// IO is what a verb reads and writes.
type IO struct {
	Args   []string // arguments after <group> <verb>
	Stdin  io.Reader
	Stdout io.Writer
	Stderr io.Writer
}

// Handler runs one verb: it reads its request from io.Stdin and writes its response to
// io.Stdout. A *DomainError return is reported as a domain error (exit 1); any other error
// means the core broke (exit 2).
type Handler func(io IO) error

// DomainError is a failure of the operation itself (a bad request, a refused deploy), not of the
// core. It is written to stdout as {"error_kind": ..., "message": ...} and exits 1.
type DomainError struct {
	Kind    string
	Message string
}

func (e *DomainError) Error() string { return e.Kind + ": " + e.Message }

var registry = map[string]map[string]Handler{}

// register adds the verb group/verb; a group file calls it from init.
func register(group, verb string, h Handler) {
	if registry[group] == nil {
		registry[group] = map[string]Handler{}
	}
	if registry[group][verb] != nil {
		panic("vibesys-evaluator: duplicate subcommand " + group + " " + verb)
	}
	registry[group][verb] = h
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) < 2 {
		fmt.Fprint(stderr, usage())
		return exitCore
	}
	h := registry[args[0]][args[1]]
	if h == nil {
		fmt.Fprintf(stderr, "vibesys-evaluator: unknown subcommand %q %q\n%s", args[0], args[1], usage())
		return exitCore
	}
	err := h(IO{Args: args[2:], Stdin: stdin, Stdout: stdout, Stderr: stderr})
	var domain *DomainError
	switch {
	case err == nil:
		return exitOK
	case errors.As(err, &domain):
		_ = json.NewEncoder(stdout).Encode(map[string]string{"error_kind": domain.Kind, "message": domain.Message})
		fmt.Fprintln(stderr, "vibesys-evaluator:", domain.Error())
		return exitDomain
	default:
		fmt.Fprintln(stderr, "vibesys-evaluator:", err)
		return exitCore
	}
}

func usage() string {
	groups := make([]string, 0, len(registry))
	for g := range registry {
		groups = append(groups, g)
	}
	sort.Strings(groups)
	out := "usage: vibesys-evaluator <group> <verb>   (request on stdin, response on stdout)\n"
	for _, g := range groups {
		verbs := make([]string, 0, len(registry[g]))
		for v := range registry[g] {
			verbs = append(verbs, v)
		}
		sort.Strings(verbs)
		for _, v := range verbs {
			out += "  " + g + " " + v + "\n"
		}
	}
	return out
}
