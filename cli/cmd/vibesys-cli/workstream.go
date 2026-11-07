package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"strconv"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

const workstreamUsage = "workstream: name a subcommand: start, list or edit"

// workstreamCommand is `workstream start|list|edit`. A workstream is a line of work an agent names,
// such as "KV cache layout", which plans join with `plan create --workstream`. The daemon records
// who started it and when, and an agent states its status and a one-line note; the CLI orders no
// status and counts nothing, since what a workstream holds is read from its plans.
func workstreamCommand(ctx context.Context, d deps, args []string) int {
	if len(args) == 0 {
		return fail(d, d.jsonErrors, usagef("%s", workstreamUsage))
	}
	switch args[0] {
	case "start":
		return workstreamStart(ctx, d, args[1:])
	case "list":
		return workstreamList(ctx, d, args[1:])
	case "edit":
		return workstreamEdit(ctx, d, args[1:])
	}
	return fail(d, d.jsonErrors, usagef("workstream: %q is not a subcommand (start, list or edit)", args[0]))
}

// workstreamStatusNames maps what a user writes to workstream statuses, read from the contract's
// enum so a status added there is a status here.
var workstreamStatusNames = enumNames(cliv1.WorkstreamStatus_name, "WORKSTREAM_STATUS_", func(n int32) cliv1.WorkstreamStatus { return cliv1.WorkstreamStatus(n) })

func workstreamStatusChoices() string {
	return choices(cliv1.WorkstreamStatus_name, "WORKSTREAM_STATUS_")
}

func workstreamStatusName(s cliv1.WorkstreamStatus) string {
	return strings.ToLower(strings.TrimPrefix(s.String(), "WORKSTREAM_STATUS_"))
}

func parseWorkstreamStatus(flagName, name string) (cliv1.WorkstreamStatus, error) {
	s, ok := workstreamStatusNames[strings.TrimSpace(name)]
	if !ok {
		return 0, usagef("%s: %q is not a workstream status (%s)", flagName, name, workstreamStatusChoices())
	}
	return s, nil
}

// workstreamStatusList is a repeatable flag that also takes a comma-separated list.
type workstreamStatusList []cliv1.WorkstreamStatus

func (l *workstreamStatusList) String() string { return fmt.Sprint([]cliv1.WorkstreamStatus(*l)) }

func (l *workstreamStatusList) Set(v string) error {
	for _, name := range strings.Split(v, ",") {
		s, err := parseWorkstreamStatus("status", name)
		if err != nil {
			return errors.New(err.Error())
		}
		*l = append(*l, s)
	}
	return nil
}

// parseWorkstreamNumber reads a workstream number as `3` or `#3`, from the argument of the flag, or
// from the positional argument when flagName is empty.
func parseWorkstreamNumber(flagName, arg string) (uint32, error) {
	n, err := strconv.ParseUint(strings.TrimPrefix(strings.TrimSpace(arg), "#"), 10, 32)
	if err != nil || n == 0 {
		what := "a workstream number: give a number such as 3, from workstream list"
		if flagName == "" {
			return 0, usagef("%q is not %s", arg, what)
		}
		return 0, usagef("%s: %q is not %s", flagName, arg, what)
	}
	return uint32(n), nil
}

// workstreamFlag is the --workstream flag of plan create and plan edit.
type workstreamFlag struct{ value optString }

func addWorkstreamFlag(fs *flag.FlagSet, what string) *workstreamFlag {
	w := &workstreamFlag{}
	fs.Var(&w.value, "workstream", what+"; a number from workstream list, or from workstream start")
	return w
}

// read returns the workstream number the flag gave, or 0 when it was not given.
func (w *workstreamFlag) read() (uint32, error) {
	if !w.value.set {
		return 0, nil
	}
	return parseWorkstreamNumber("--workstream", w.value.value)
}

// describeWorkstream is the one line a workstream takes: its number first, as a plan's is, so a
// caller can read it back, then its status and its title, and its note when it has one.
func describeWorkstream(w *cliv1.Workstream) string {
	line := fmt.Sprintf("%d\t%s\t%s", w.GetNumber(), workstreamStatusName(w.GetStatus()), w.GetTitle())
	if w.GetNote() != "" {
		line += "\t" + w.GetNote()
	}
	return line
}

// workstreamStart is `workstream start <title>`. A title that a workstream that is not closed has,
// ignoring case and white space, is that workstream: the line is the same and the number is the
// existing one, so planners that name the same line of work at once agree on one.
func workstreamStart(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("workstream start", d)
	positional, code, ok := planArgs(fs, args)
	if !ok {
		return code
	}
	if len(positional) != 1 {
		return fail(d, c.json, usagef(`workstream start: give the workstream's title as one argument, such as workstream start "KV cache layout"`))
	}
	callCtx, cl, err := planClient(ctx, d, c)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.StartWorkstream(ctx, &cliv1.StartWorkstreamRequest{Context: callCtx, Title: positional[0]})
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
		return exitOK
	}
	fmt.Fprintln(d.stdout, describeWorkstream(resp.GetWorkstream()))
	return exitOK
}

// workstreamList is `workstream list [--status s]`. Without --status it lists every workstream.
func workstreamList(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("workstream list", d)
	var statuses workstreamStatusList
	fs.Var(&statuses, "status", "only workstreams with this status ("+workstreamStatusChoices()+"); repeat or separate with commas")
	positional, code, ok := planArgs(fs, args)
	if !ok {
		return code
	}
	if len(positional) != 0 {
		return fail(d, c.json, usagef("workstream list: unexpected argument %q", positional[0]))
	}
	callCtx, cl, err := planClient(ctx, d, c)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.ListWorkstreams(ctx, &cliv1.ListWorkstreamsRequest{Context: callCtx, Statuses: statuses})
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
		return exitOK
	}
	for _, w := range resp.GetWorkstreams() {
		fmt.Fprintln(d.stdout, describeWorkstream(w))
	}
	return exitOK
}

// workstreamEdit is `workstream edit <n> [--title t] [--status s] [--note text]`. It changes what it
// is given and leaves the rest; an empty --note clears the note.
func workstreamEdit(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("workstream edit", d)
	var title, status, note optString
	fs.Var(&title, "title", "a new title, one line")
	fs.Var(&status, "status", "a new status ("+workstreamStatusChoices()+")")
	fs.Var(&note, "note", `a one-line state of the line, such as "paused: accuracy gate"; empty clears it`)
	positional, code, ok := planArgs(fs, args)
	if !ok {
		return code
	}
	if len(positional) != 1 {
		return fail(d, c.json, usagef("workstream edit: give one workstream number"))
	}
	number, err := parseWorkstreamNumber("", positional[0])
	if err != nil {
		return fail(d, c.json, err)
	}
	req := &cliv1.EditWorkstreamRequest{Number: number}
	if title.set {
		req.Title = &title.value
	}
	if note.set {
		req.Note = &note.value
	}
	if status.set {
		s, err := parseWorkstreamStatus("--status", status.value)
		if err != nil {
			return fail(d, c.json, err)
		}
		req.Status = &s
	}
	if req.Title == nil && req.Status == nil && req.Note == nil {
		return fail(d, c.json, usagef("workstream edit: give at least one of --title, --status or --note"))
	}
	callCtx, cl, err := planClient(ctx, d, c)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	req.Context = callCtx
	resp, err := cl.EditWorkstream(ctx, req)
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
		return exitOK
	}
	fmt.Fprintln(d.stdout, describeWorkstream(resp.GetWorkstream()))
	return exitOK
}
