package main

import (
	"context"
	"fmt"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// labCommand is `lab <subcommand> ...`: a lab holds a placement's containers, the candidate's image
// over a clean checkout and the task's beside it, and runs in them whatever it is asked, for
// profiling and exploring. Nothing a lab runs is scored.
func labCommand(ctx context.Context, d deps, args []string) int {
	subs := map[string]func(context.Context, deps, []string) int{
		"start": labStart, "exec": labExec, "ps": labPs, "kill": labKill, "checkout": labCheckout,
		"cp": labCp, "end": labEnd, "list": labList,
	}
	if len(args) == 0 {
		return fail(d, d.jsonErrors, usagef("lab: name a subcommand: start, exec, ps, kill, checkout, cp, end or list"))
	}
	sub, ok := subs[args[0]]
	if !ok {
		return fail(d, d.jsonErrors, usagef("lab: %q is not a subcommand (start, exec, ps, kill, checkout, cp, end or list)", args[0]))
	}
	return sub(ctx, d, args[1:])
}

// labStart is `lab start <commit> [--for 30m] [--wait] [--note t] [--plan n]`.
func labStart(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("lab start", d)
	var (
		forTime = fs.Duration("for", 0, "how long the lab lasts once it is ready, such as 45m (default 30m, at most 24h; a placement's cap shortens it)")
		note    = fs.String("note", "", "a note to attach to the lab's job")
		plan    = fs.Uint("plan", 0, "the number of a plan to link the lab to")
		wait    = fs.Bool("wait", false, "block until the lab is ready (or ended), then print where it is")
	)
	positional, err := parse(fs, args)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	switch {
	case len(positional) != 1:
		return fail(d, c.json, usagef("lab start: give one commit"))
	case *forTime < 0:
		return fail(d, c.json, usagef("--for: %v is negative", *forTime))
	}
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	commit, err := d.commit(ctx, callCtx, positional[0])
	if err != nil {
		return fail(d, c.json, err)
	}
	settings := &cliv1.CommandSettings{Lab: &cliv1.LabSpec{}}
	if *forTime > 0 {
		settings.Timeout = durationpb.New(*forTime)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.Submit(ctx, &cliv1.SubmitRequest{
		Context: callCtx, Note: *note, Plan: uint32(*plan),
		Spec: &cliv1.JobSpec{
			Commit: commit, Environment: cliv1.EnvironmentKind_ENVIRONMENT_KIND_DOCKER, Placement: c.placement,
			Settings: &cliv1.JobSpec_Command{Command: settings},
		},
	})
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
	} else {
		fmt.Fprintln(d.stdout, describe(resp.GetJob()))
	}
	if !*wait {
		return exitOK
	}
	return labStatus(ctx, d, cl, callCtx, resp.GetJob().GetId(), true, c.json)
}

// labStatus prints where a lab is, waiting first until it is ready or ended when asked to.
func labStatus(ctx context.Context, d deps, cl *client.Client, callCtx *cliv1.CallContext, job string, wait, asJSON bool) int {
	var status *cliv1.LabStatus
	err := cl.Lab(ctx, labRequest(callCtx, job, &cliv1.LabCall{Op: &cliv1.LabCall_Status{Status: &cliv1.LabStatusRequest{Wait: wait}}}), func(r *cliv1.LabReply) error {
		status = r.GetStatus()
		return nil
	})
	if err == nil && status == nil {
		err = labNoAnswer()
	}
	if err != nil {
		return fail(d, asJSON, err)
	}
	if asJSON {
		printJSON(d, status)
	} else {
		fmt.Fprint(d.stdout, describeLab(job, status))
	}
	if status.GetPhase() == cliv1.LabPhase_LAB_PHASE_ENDED && wait {
		return exitFailed
	}
	return exitOK
}

func labRequest(callCtx *cliv1.CallContext, job string, call *cliv1.LabCall) *cliv1.LabRequest {
	return &cliv1.LabRequest{Context: callCtx, JobId: job, Call: call}
}

func labNoAnswer() error { return &unavailable{message: "the lab gave no answer to the call"} }

// describeLab is a lab's status as `lab ps` prints it.
func describeLab(job string, s *cliv1.LabStatus) string {
	var b strings.Builder
	phase := strings.ToLower(strings.TrimPrefix(s.GetPhase().String(), "LAB_PHASE_"))
	fmt.Fprintf(&b, "%s\t%s", job, phase)
	if commit := s.GetCommit(); commit != "" {
		fmt.Fprintf(&b, "\t%s", commit[:min(12, len(commit))])
	}
	if s.GetPhase() == cliv1.LabPhase_LAB_PHASE_READY {
		fmt.Fprintf(&b, "\tuntil %s", s.GetUntil().AsTime().UTC().Format(time.RFC3339))
		rooms := make([]string, 0, len(s.GetRooms()))
		for _, r := range s.GetRooms() {
			rooms = append(rooms, roomName(r))
		}
		reach := ""
		if len(rooms) > 1 {
			reach = "; the task's reaches it at 127.0.0.1"
		}
		fmt.Fprintf(&b, "\n\trooms: %s\n\tport: %d (free in the candidate's container%s)\n\tartifacts: %s",
			strings.Join(rooms, ", "), s.GetPort(), reach, s.GetArtifacts())
		if env := wire.LabServiceEnv(s); env != "" {
			fmt.Fprintf(&b, "\n\tservice: %s (in the task's container)", env)
		}
	}
	b.WriteString("\n")
	for _, p := range s.GetProcesses() {
		fmt.Fprintf(&b, "%s\n", describeProcess(p))
	}
	return b.String()
}

func describeProcess(p *cliv1.LabProcess) string {
	state := "running"
	switch st := p.GetState().(type) {
	case *cliv1.LabProcess_ExitCode:
		state = fmt.Sprintf("exited %d", st.ExitCode)
	case *cliv1.LabProcess_Lost:
		state = "lost: " + st.Lost
	}
	return fmt.Sprintf("%s\t%s\t%s\t%s", p.GetHandle(), roomName(p.GetRoom()), state, commandSummary(p.GetArgv()))
}

func roomName(r cliv1.LabRoom) string {
	return strings.ToLower(strings.TrimPrefix(r.String(), "LAB_ROOM_"))
}

// roomNames maps what a user writes to rooms, read from the contract's enum.
var roomNames = enumNames(cliv1.LabRoom_name, "LAB_ROOM_", func(n int32) cliv1.LabRoom { return cliv1.LabRoom(n) })

// envList is a repeatable --env K=V.
type envList map[string]string

func (l envList) String() string { return fmt.Sprint(map[string]string(l)) }

func (l envList) Set(v string) error {
	k, val, ok := strings.Cut(v, "=")
	if !ok || k == "" {
		return fmt.Errorf("%q is not NAME=value", v)
	}
	l[k] = val
	return nil
}

// labExec is `lab exec <id> [--in candidate|task] [--detach] [--env K=V]... -- <argv...>`. In the
// foreground it streams the command's output and exits with its exit code.
func labExec(ctx context.Context, d deps, args []string) int {
	flagArgs, argv, hasCommand := splitDashes(args)
	fs, c := newFlags("lab exec", d)
	env := envList{}
	var (
		in     = fs.String("in", "candidate", "the container to run in: candidate (the candidate's image, with the GPUs) or task (the task's image of a task of two images)")
		detach = fs.Bool("detach", false, "start it in the background and print its handle; its output goes to the lab's log (vibesys-cli logs <id>)")
	)
	fs.Var(env, "env", "a variable to set for the command, NAME=value; repeat for more")
	positional, err := parse(fs, flagArgs)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	room, known := roomNames[*in]
	switch {
	case len(positional) != 1:
		return fail(d, c.json, usagef("lab exec: name one lab, then -- and the command"))
	case !hasCommand || len(argv) == 0:
		return fail(d, c.json, usagef("lab exec: give the command to run after --; a shell is a command: -- sh -c '...'"))
	case !known:
		return fail(d, c.json, usagef("--in: %q is not a container of a lab (%s)", *in, choices(cliv1.LabRoom_name, "LAB_ROOM_")))
	}
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	exec := &cliv1.LabExec{Room: room, Argv: argv, Env: env, Detach: *detach}
	var exit *cliv1.LabExit
	var started *cliv1.LabProcess
	err = cl.Lab(ctx, labRequest(callCtx, positional[0], &cliv1.LabCall{Op: &cliv1.LabCall_Exec{Exec: exec}}), func(r *cliv1.LabReply) error {
		if c.json {
			printJSON(d, r)
		}
		switch item := r.GetItem().(type) {
		case *cliv1.LabReply_Output:
			if !c.json {
				_, err := d.stdout.Write(item.Output)
				return err
			}
		case *cliv1.LabReply_Exit:
			exit = item.Exit
		case *cliv1.LabReply_Process:
			started = item.Process
		}
		return nil
	})
	switch {
	case err != nil:
		return fail(d, c.json, err)
	case started != nil:
		if !c.json {
			fmt.Fprintln(d.stdout, describeProcess(started))
		}
		return exitOK
	case exit == nil:
		return fail(d, c.json, labNoAnswer())
	case exit.GetLost() != "":
		if !c.json {
			fmt.Fprintf(d.stderr, "vibesys-cli: the command started, and how it ended is not known: %s\n", exit.GetLost())
		}
		return exitNoAnswer
	}
	return int(exit.GetCode())
}

// labPs is `lab ps <id>`.
func labPs(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("lab ps", d)
	wait := fs.Bool("wait", false, "wait until the lab is ready or ended")
	positional, err := parse(fs, args)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	if len(positional) != 1 {
		return fail(d, c.json, usagef("lab ps: name one lab"))
	}
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	return labStatus(ctx, d, cl, callCtx, positional[0], *wait, c.json)
}

// labKill is `lab kill <id> <handle> [--signal term|kill]`.
func labKill(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("lab kill", d)
	signal := fs.String("signal", "term", "term (SIGTERM, to clean up) or kill (SIGKILL); for another signal, lab exec -- kill -s <signal> <pid>")
	positional, err := parse(fs, args)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	signals := enumNames(cliv1.LabSignal_name, "LAB_SIGNAL_", func(n int32) cliv1.LabSignal { return cliv1.LabSignal(n) })
	sig, known := signals[strings.ToLower(*signal)]
	switch {
	case len(positional) != 2:
		return fail(d, c.json, usagef("lab kill: name one lab and one of its processes, such as p2"))
	case !known:
		return fail(d, c.json, usagef("--signal: %q is not a signal lab kill sends (%s); send another with lab exec -- kill -s <signal> <pid>", *signal, choices(cliv1.LabSignal_name, "LAB_SIGNAL_")))
	}
	return labAnswer(ctx, d, c, positional[0], &cliv1.LabCall{Op: &cliv1.LabCall_Kill{Kill: &cliv1.LabKill{Handle: positional[1], Signal: sig}}})
}

// labCheckout is `lab checkout <id> <commit>`.
func labCheckout(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("lab checkout", d)
	positional, err := parse(fs, args)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	if len(positional) != 2 {
		return fail(d, c.json, usagef("lab checkout: name one lab and one commit"))
	}
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	commit, err := d.commit(ctx, callCtx, positional[1])
	if err != nil {
		return fail(d, c.json, err)
	}
	return labAnswer(ctx, d, c, positional[0], &cliv1.LabCall{Op: &cliv1.LabCall_Checkout{Checkout: &cliv1.LabCheckout{Commit: commit}}})
}

// labEnd is `lab end <id>`: the lab ends now, and the command returns once its artifacts are kept.
func labEnd(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("lab end", d)
	positional, err := parse(fs, args)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	if len(positional) != 1 {
		return fail(d, c.json, usagef("lab end: name one lab"))
	}
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	err = cl.Lab(ctx, labRequest(callCtx, positional[0], &cliv1.LabCall{Op: &cliv1.LabCall_End{End: &cliv1.LabEnd{}}}), func(*cliv1.LabReply) error { return nil })
	if err != nil {
		return fail(d, c.json, err)
	}
	return waitFor(ctx, d, cl, callCtx, []string{positional[0]}, client.WaitOptions{}, c.json)
}

// labAnswer makes a call whose answer is one message, and prints it.
func labAnswer(ctx context.Context, d deps, c *common, job string, call *cliv1.LabCall) int {
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	var answer *cliv1.LabReply
	err = cl.Lab(ctx, labRequest(callCtx, job, call), func(r *cliv1.LabReply) error {
		answer = r
		return nil
	})
	if err == nil && answer == nil {
		err = labNoAnswer()
	}
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, answer)
		return exitOK
	}
	switch item := answer.GetItem().(type) {
	case *cliv1.LabReply_Process:
		fmt.Fprintln(d.stdout, describeProcess(item.Process))
	case *cliv1.LabReply_Status:
		fmt.Fprint(d.stdout, describeLab(job, item.Status))
	}
	return exitOK
}

// labSide is an argument of lab cp that names a lab's path: <job>:<path>.
var labSide = regexp.MustCompile(`^(j[0-9]+):(.*)$`)

// labCp is `lab cp <id>:<path> <local>` and `lab cp <local> <id>:<path>`. A lab's path is in its
// artifacts directory, which its containers share at /vibesys/artifacts: relative to it, or
// absolute under it.
func labCp(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("lab cp", d)
	positional, err := parse(fs, args)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	if len(positional) != 2 {
		return fail(d, c.json, usagef("lab cp: give <lab>:<path> <local> or <local> <lab>:<path>"))
	}
	from, to := labSide.FindStringSubmatch(positional[0]), labSide.FindStringSubmatch(positional[1])
	if (from == nil) == (to == nil) {
		return fail(d, c.json, usagef("lab cp: exactly one side names a lab, as j7:profile/out.json"))
	}
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	var copied client.Copied
	if from != nil {
		rel, err := labPath(from[2])
		if err != nil {
			return fail(d, c.json, err)
		}
		copied, err = cl.Pull(ctx, callCtx, from[1], rel, d.abs(positional[1]))
		if err != nil {
			return fail(d, c.json, err)
		}
	} else {
		rel, err := labPath(to[2])
		if err != nil {
			return fail(d, c.json, err)
		}
		if rel == "" {
			rel = filepath.Base(positional[0])
		}
		copied, err = cl.Push(ctx, callCtx, to[1], d.abs(positional[0]), rel)
		if err != nil {
			return fail(d, c.json, err)
		}
	}
	if c.json {
		printJSON(d, &cliv1.LabPulled{Files: uint32(copied.Files), Bytes: copied.Bytes})
	} else {
		fmt.Fprintf(d.stdout, "copied %d files, %d bytes\n", copied.Files, copied.Bytes)
	}
	return exitOK
}

// onlyLabs keeps the labs of a listing of command jobs, and why each that waits does.
func onlyLabs(resp *cliv1.ListJobsResponse) *cliv1.ListJobsResponse {
	labs := &cliv1.ListJobsResponse{}
	ids := map[string]bool{}
	for _, j := range resp.GetJobs() {
		if j.GetSpec().GetCommand().GetLab() != nil {
			labs.Jobs = append(labs.Jobs, j)
			ids[j.GetId()] = true
		}
	}
	for _, w := range resp.GetWaits() {
		if ids[w.GetJobId()] {
			labs.Waits = append(labs.Waits, w)
		}
	}
	return labs
}

// labArtifacts is where a lab's containers see its artifacts directory.
const labArtifacts = "/vibesys/artifacts"

// labPath is a lab's path as the daemon takes it: relative to the artifacts directory and clean.
func labPath(p string) (string, error) {
	clean := filepath.ToSlash(filepath.Clean(p))
	if filepath.IsAbs(p) {
		rel, ok := strings.CutPrefix(clean, labArtifacts)
		if !ok || (rel != "" && rel[0] != '/') {
			return "", usagef("lab cp: %q is not in the lab's artifacts directory %s; copy it there first with lab exec -- cp", p, labArtifacts)
		}
		clean = strings.TrimPrefix(rel, "/")
	}
	switch {
	case clean == "." || clean == "":
		return "", nil
	case clean == ".." || strings.HasPrefix(clean, "../"):
		return "", usagef("lab cp: %q climbs out of the lab's artifacts directory", p)
	}
	return clean, nil
}

// abs is a local path of the command line as an absolute one.
func (d deps) abs(p string) string {
	if filepath.IsAbs(p) {
		return p
	}
	return filepath.Join(d.cwd, p)
}

// labList is `lab list [--state s]`: the task's labs, oldest first.
func labList(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("lab list", d)
	var states stateList
	fs.Var(&states, "state", "only labs in this state; repeat or separate with commas")
	positional, err := parse(fs, args)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	if len(positional) != 0 {
		return fail(d, c.json, usagef("lab list: unexpected argument %q", positional[0]))
	}
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.ListJobs(ctx, &cliv1.ListJobsRequest{Context: callCtx, States: states, Kind: cliv1.JobKind_JOB_KIND_COMMAND})
	if err != nil {
		return fail(d, c.json, err)
	}
	labs := onlyLabs(resp)
	if c.json {
		printJSON(d, labs)
		return exitOK
	}
	printJobs(d, labs, callCtx.GetTask())
	return exitOK
}
