package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"sort"
	"strconv"
	"strings"
	"time"

	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
)

const planUsage = "plan: name a subcommand: create, list, view, edit, comment or close"

// planCommand is `plan create|list|view|edit|comment|close`, in the style of `gh issue`. Plans
// belong to a task. The CLI stores what an agent writes in them and says nothing about it.
func planCommand(ctx context.Context, d deps, args []string) int {
	if len(args) == 0 {
		return fail(d, false, usagef("%s", planUsage))
	}
	sub, ok := planSubcommands[args[0]]
	if !ok {
		return fail(d, false, usagef("plan: %q is not a subcommand (create, list, view, edit, comment or close)", args[0]))
	}
	return sub(ctx, d, args[1:])
}

var planSubcommands = map[string]func(context.Context, deps, []string) int{
	"create":  planCreate,
	"list":    planList,
	"view":    planView,
	"edit":    planEdit,
	"comment": planComment,
	"close":   planClose,
}

// planStatusNames maps what a user writes to plan statuses. It is read from the contract's enum,
// so a status added there is a status here.
var planStatusNames = func() map[string]cliv1.PlanStatus {
	names := map[string]cliv1.PlanStatus{}
	for n, name := range cliv1.PlanStatus_name {
		if s := cliv1.PlanStatus(n); s != cliv1.PlanStatus_PLAN_STATUS_UNSPECIFIED {
			names[strings.ToLower(strings.TrimPrefix(name, "PLAN_STATUS_"))] = s
		}
	}
	return names
}()

// planStatusChoices lists the statuses in the order the contract numbers them.
func planStatusChoices() string {
	names := make([]string, 0, len(planStatusNames))
	for name := range planStatusNames {
		names = append(names, name)
	}
	sort.Slice(names, func(i, j int) bool { return planStatusNames[names[i]] < planStatusNames[names[j]] })
	return strings.Join(names, ", ")
}

func planStatusName(s cliv1.PlanStatus) string {
	return strings.ToLower(strings.TrimPrefix(s.String(), "PLAN_STATUS_"))
}

func parsePlanStatus(flagName, name string) (cliv1.PlanStatus, error) {
	s, ok := planStatusNames[strings.TrimSpace(name)]
	if !ok {
		return 0, usagef("%s: %q is not a plan status (%s)", flagName, name, planStatusChoices())
	}
	return s, nil
}

// planStatusList is a repeatable flag that also takes a comma-separated list.
type planStatusList []cliv1.PlanStatus

func (l *planStatusList) String() string { return fmt.Sprint([]cliv1.PlanStatus(*l)) }

func (l *planStatusList) Set(v string) error {
	for _, name := range strings.Split(v, ",") {
		s, err := parsePlanStatus("status", name)
		if err != nil {
			return errors.New(err.Error())
		}
		*l = append(*l, s)
	}
	return nil
}

// parsePlanNumber reads a plan number as `3` or `#3`.
func parsePlanNumber(arg string) (uint32, error) {
	n, err := strconv.ParseUint(strings.TrimPrefix(arg, "#"), 10, 32)
	if err != nil || n == 0 {
		return 0, usagef("%q is not a plan number: give a number such as 3", arg)
	}
	return uint32(n), nil
}

// maxInputBytes bounds the text read from a file or from standard input. The daemon holds each
// text to a tighter limit of its own; this one keeps a mistaken path, such as a binary file, from
// being read whole.
const maxInputBytes = 1 << 20

// textFlag is a text an agent may give in three ways, as gh does: `--<name> text`, `--<name> -` for
// standard input, or `--<name>-file path` with `-` for standard input. Giving it in two ways is a
// mistake.
type textFlag struct {
	name       string
	text, file optString
}

// optString is a string flag that knows whether it was given, so `--body ""` is a body.
type optString struct {
	value string
	set   bool
}

func (o *optString) String() string { return o.value }

func (o *optString) Set(v string) error {
	o.value, o.set = v, true
	return nil
}

func addTextFlag(fs *flag.FlagSet, name, what string) *textFlag {
	t := &textFlag{name: name}
	fs.Var(&t.text, name, what+`; "-" reads standard input`)
	fs.Var(&t.file, name+"-file", "read "+what+` from a file; "-" reads standard input`)
	return t
}

// read returns the text and whether one was given.
func (t *textFlag) read(d deps) (string, bool, error) {
	switch {
	case t.text.set && t.file.set:
		return "", false, usagef("--%s and --%s-file: give the text one way", t.name, t.name)
	case t.file.set:
		return readInput(d, "--"+t.name+"-file", t.file.value)
	case t.text.set && t.text.value == "-":
		return readInput(d, "--"+t.name, "-")
	case t.text.set:
		return t.text.value, true, nil
	default:
		return "", false, nil
	}
}

func readInput(d deps, flagName, path string) (string, bool, error) {
	var r io.Reader
	if path == "-" {
		r = d.stdin
	} else {
		raw, err := d.readFile(path)
		if err != nil {
			return "", false, usagef("%s: %v", flagName, err)
		}
		r = strings.NewReader(string(raw))
	}
	raw, err := io.ReadAll(io.LimitReader(r, maxInputBytes+1))
	if err != nil {
		return "", false, usagef("%s: %v", flagName, err)
	}
	if len(raw) > maxInputBytes {
		return "", false, usagef("%s: the text is over %d bytes", flagName, maxInputBytes)
	}
	return string(raw), true, nil
}

// planArgs parses the flags of a subcommand and returns its positional arguments. The second
// result is false when the caller is to return the code at once.
func planArgs(fs *flag.FlagSet, args []string) ([]string, int, bool) {
	positional, err := parse(fs, args)
	if err != nil {
		if isHelp(err) {
			return nil, exitOK, false
		}
		return nil, exitInvalidInput, false
	}
	return positional, 0, true
}

// planTarget parses the one plan number a subcommand takes.
func planTarget(sub string, positional []string) (uint32, error) {
	if len(positional) != 1 {
		return 0, usagef("plan %s: give one plan number", sub)
	}
	return parsePlanNumber(positional[0])
}

// planClient makes the call context of a plan command and connects to the daemon. The caller
// closes the client.
func planClient(ctx context.Context, d deps, c *common) (*cliv1.CallContext, *client.Client, error) {
	callCtx, err := c.callContext(ctx, d)
	if err != nil {
		return nil, nil, err
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return nil, nil, err
	}
	return callCtx, cl, nil
}

// planResponse is a response that holds one plan.
type planResponse interface {
	proto.Message
	GetPlan() *cliv1.Plan
}

// printPlan writes a plan: as the response with --json, and otherwise as its one line.
func printPlan(d deps, asJSON bool, resp planResponse) {
	if asJSON {
		printJSON(d, resp)
		return
	}
	fmt.Fprintln(d.stdout, describePlan(resp.GetPlan()))
}

// describePlan is the one line a plan takes: its number first, as a job's id is, so a caller can
// read it back.
func describePlan(p *cliv1.Plan) string {
	return fmt.Sprintf("%d\t%s\t%s", p.GetNumber(), planStatusName(p.GetStatus()), p.GetTitle())
}

// planCreate is `plan create --title t [--body text | --body-file path]`.
func planCreate(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("plan create", d)
	title := fs.String("title", "", "the plan's title, one line")
	body := addTextFlag(fs, "body", "the plan's body, in markdown")
	positional, code, ok := planArgs(fs, args)
	if !ok {
		return code
	}
	switch {
	case len(positional) != 0:
		return fail(d, c.json, usagef("plan create: unexpected argument %q; give the title with --title", positional[0]))
	case strings.TrimSpace(*title) == "":
		return fail(d, c.json, usagef("--title: give the plan a title"))
	}
	text, _, err := body.read(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	callCtx, cl, err := planClient(ctx, d, c)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.CreatePlan(ctx, &cliv1.CreatePlanRequest{Context: callCtx, Title: *title, Body: text})
	if err != nil {
		return fail(d, c.json, err)
	}
	printPlan(d, c.json, resp)
	return exitOK
}

// planList is `plan list [--status s] [--search text]`. Without --status it lists every plan.
func planList(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("plan list", d)
	var statuses planStatusList
	search := fs.String("search", "", "only plans whose title or body contains this text")
	fs.Var(&statuses, "status", "only plans with this status ("+planStatusChoices()+"); repeat or separate with commas")
	positional, code, ok := planArgs(fs, args)
	if !ok {
		return code
	}
	if len(positional) != 0 {
		return fail(d, c.json, usagef("plan list: unexpected argument %q", positional[0]))
	}
	callCtx, cl, err := planClient(ctx, d, c)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.ListPlans(ctx, &cliv1.ListPlansRequest{Context: callCtx, Statuses: statuses, Text: *search})
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
		return exitOK
	}
	for _, p := range resp.GetPlans() {
		fmt.Fprintln(d.stdout, describePlan(p))
	}
	return exitOK
}

// planView is `plan view <number>`: the plan, its comments and the jobs linked to it.
func planView(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("plan view", d)
	positional, code, ok := planArgs(fs, args)
	if !ok {
		return code
	}
	number, err := planTarget("view", positional)
	if err != nil {
		return fail(d, c.json, err)
	}
	callCtx, cl, err := planClient(ctx, d, c)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.ViewPlan(ctx, &cliv1.ViewPlanRequest{Context: callCtx, Number: number})
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
	} else {
		fmt.Fprint(d.stdout, renderPlanView(resp.GetView()))
	}
	return exitOK
}

// renderPlanView is a plan as text: the line, the times, the body, then the comments and the jobs.
func renderPlanView(v *cliv1.PlanView) string {
	p := v.GetPlan()
	var b strings.Builder
	fmt.Fprintf(&b, "%s\n", describePlan(p))
	fmt.Fprintf(&b, "created %s, updated %s\n", clockText(p.GetCreatedAt().AsTime()), clockText(p.GetUpdatedAt().AsTime()))
	if p.GetBody() != "" {
		fmt.Fprintf(&b, "\n%s\n", strings.TrimRight(p.GetBody(), "\n"))
	}
	if cs := p.GetComments(); len(cs) > 0 {
		fmt.Fprintf(&b, "\ncomments (%d):\n", len(cs))
		for _, cm := range cs {
			who := cm.GetAgent()
			if who == "" {
				who = "unknown agent"
			}
			fmt.Fprintf(&b, "\n  %s, %s\n", who, clockText(cm.GetAt().AsTime()))
			for _, line := range strings.Split(strings.TrimRight(cm.GetText(), "\n"), "\n") {
				fmt.Fprintf(&b, "  %s\n", line)
			}
		}
	}
	if js := v.GetJobs(); len(js) > 0 {
		fmt.Fprintf(&b, "\njobs (%d):\n", len(js))
		for _, j := range js {
			fmt.Fprintf(&b, "%s\n", describe(j.GetJob()))
		}
	}
	return b.String()
}

func clockText(t time.Time) string { return t.UTC().Format(time.RFC3339) }

// planEdit is `plan edit <number> [--title t] [--body text | --body-file path] [--status s]`. It
// changes what it is given and leaves the rest.
func planEdit(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("plan edit", d)
	var title, status optString
	fs.Var(&title, "title", "a new title, one line")
	fs.Var(&status, "status", "a new status ("+planStatusChoices()+")")
	body := addTextFlag(fs, "body", "a new body, in markdown")
	positional, code, ok := planArgs(fs, args)
	if !ok {
		return code
	}
	number, err := planTarget("edit", positional)
	if err != nil {
		return fail(d, c.json, err)
	}
	req := &cliv1.EditPlanRequest{Number: number}
	if title.set {
		req.Title = &title.value
	}
	if status.set {
		s, err := parsePlanStatus("--status", status.value)
		if err != nil {
			return fail(d, c.json, err)
		}
		req.Status = &s
	}
	if text, given, err := body.read(d); err != nil {
		return fail(d, c.json, err)
	} else if given {
		req.Body = &text
	}
	if req.Title == nil && req.Body == nil && req.Status == nil {
		return fail(d, c.json, usagef("plan edit: give at least one of --title, --body, --body-file or --status"))
	}
	callCtx, cl, err := planClient(ctx, d, c)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	req.Context = callCtx
	resp, err := cl.EditPlan(ctx, req)
	if err != nil {
		return fail(d, c.json, err)
	}
	printPlan(d, c.json, resp)
	return exitOK
}

// planComment is `plan comment <number> [--body text | --body-file path]`.
func planComment(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("plan comment", d)
	body := addTextFlag(fs, "body", "the comment, in markdown")
	positional, code, ok := planArgs(fs, args)
	if !ok {
		return code
	}
	number, err := planTarget("comment", positional)
	if err != nil {
		return fail(d, c.json, err)
	}
	text, given, err := body.read(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	if !given {
		return fail(d, c.json, usagef("plan comment: give the comment with --body or --body-file"))
	}
	callCtx, cl, err := planClient(ctx, d, c)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.CommentPlan(ctx, &cliv1.CommentPlanRequest{Context: callCtx, Number: number, Text: text})
	if err != nil {
		return fail(d, c.json, err)
	}
	printPlan(d, c.json, resp)
	return exitOK
}

// planClose is `plan close <number> --status s [--comment text | --comment-file path]`. Any status
// but open closes; the CLI orders none of them.
func planClose(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("plan close", d)
	var status optString
	fs.Var(&status, "status", "how the plan ends ("+planStatusChoices()+", but not open)")
	comment := addTextFlag(fs, "comment", "a comment that says why, in markdown")
	positional, code, ok := planArgs(fs, args)
	if !ok {
		return code
	}
	number, err := planTarget("close", positional)
	if err != nil {
		return fail(d, c.json, err)
	}
	if !status.set {
		return fail(d, c.json, usagef("plan close: give how the plan ends with --status (%s)", planStatusChoices()))
	}
	s, err := parsePlanStatus("--status", status.value)
	if err != nil {
		return fail(d, c.json, err)
	}
	text, _, err := comment.read(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	callCtx, cl, err := planClient(ctx, d, c)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.ClosePlan(ctx, &cliv1.ClosePlanRequest{Context: callCtx, Number: number, Status: s, Comment: text})
	if err != nil {
		return fail(d, c.json, err)
	}
	printPlan(d, c.json, resp)
	return exitOK
}
