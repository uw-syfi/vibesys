package main

import (
	"context"
	"flag"
	"fmt"
	"math"
	"time"

	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
)

// submitCommand is `submit <kind> <commit> ...`.
func submitCommand(ctx context.Context, d deps, args []string) int {
	if len(args) == 0 {
		return fail(d, d.jsonErrors, usagef("submit: name the kind of job: check, measure or command"))
	}
	switch args[0] {
	case "command":
		return submitJob(ctx, d, args[1:])
	case "profile":
		return fail(d, d.jsonErrors, usagef("submit profile: retired; run the load as a command in the task's image, with the profiler wrapping the candidate's server: submit command <commit> --image harness --wrap '<profiler>' -- <load argv>"))
	case "check", "measure":
		return submitFlow(ctx, d, args[0], args[1:])
	default:
		return fail(d, d.jsonErrors, usagef("submit: %q is not a kind of job (check, measure or command)", args[0]))
	}
}

// unavailable is a refusal the command makes before it calls the daemon.
type unavailable struct{ message string }

func (u *unavailable) Error() string { return u.message }

// submitFlow is `submit check|measure <commit>`: it runs the task's own commands, so it takes no
// command of its own.
func submitFlow(ctx context.Context, d deps, kind string, args []string) int {
	flagArgs, extra, hasCommand := splitDashes(args)
	fs, c := newFlags("submit "+kind, d)
	var (
		env     = fs.String("env", "", "the environment to run in: host or docker (default: the task's)")
		note    = fs.String("note", "", "a note to attach to the job")
		noCache = fs.Bool("no-cache", false, "run the whole job again, with a fresh check, even when saved results answer it")
		plan    = fs.Uint("plan", 0, "the number of a plan to link the job to")
		noPlan  = fs.Bool("no-plan", false, "measure: the job tests no plan, as for the baseline or a re-measure of the head; not with --plan")
		wait    = fs.Bool("wait", false, "block until the job is final, then exit as `wait` does")
		samples = fs.Uint("samples", 0, "measure: how many benchmark samples are needed (default: the task's)")
		recheck = fs.Bool("recheck", false, "measure: run the check again even when a saved pass exists")
	)
	positional, err := parse(fs, flagArgs)
	if err != nil {
		if isHelp(err) {
			return exitOK
		}
		return exitInvalidInput
	}
	asJSON := c.json
	switch {
	case len(positional) != 1:
		return fail(d, asJSON, usagef("submit %s: give one commit", kind))
	case hasCommand || len(extra) > 0:
		return fail(d, asJSON, usagef("submit %s: runs the task's own commands, so it takes none after --", kind))
	case kind == "check" && *samples != 0:
		return fail(d, asJSON, usagef("--samples: a check job takes no samples"))
	case kind == "check" && *recheck:
		return fail(d, asJSON, usagef("--recheck: a check job always runs its check"))
	case kind == "check" && *noPlan:
		return fail(d, asJSON, usagef("--no-plan: only a measure job says it tests no plan; a check job takes --plan N or neither"))
	case *samples > math.MaxUint32:
		return fail(d, asJSON, usagef("--samples: %d is too many", *samples))
	}
	if err := checkPlanFlags(fs, *plan, *noPlan); err != nil {
		return fail(d, asJSON, err)
	}
	envKind, err := environmentKind(*env)
	if err == nil {
		err = refuseHostPlacement(fs, *env)
	}
	if err != nil {
		return fail(d, asJSON, err)
	}
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, asJSON, err)
	}
	commit, err := d.commit(ctx, callCtx, positional[0])
	if err != nil {
		return fail(d, asJSON, err)
	}
	spec := &cliv1.JobSpec{Commit: commit, Environment: envKind, Placement: c.placement, Allocation: c.alloc}
	if kind == "check" {
		spec.Settings = &cliv1.JobSpec_Check{Check: &cliv1.CheckSettings{}}
	} else {
		spec.Settings = &cliv1.JobSpec_Measure{Measure: &cliv1.MeasureSettings{Samples: uint32(*samples), Recheck: *recheck}}
	}
	return send(ctx, d, asJSON, *wait, &cliv1.SubmitRequest{
		Context: callCtx, Spec: spec, NoCache: *noCache, Note: *note, Plan: uint32(*plan), NoPlan: *noPlan,
	})
}

func submitJob(ctx context.Context, d deps, args []string) int {
	flagArgs, argv, hasCommand := splitDashes(args)
	fs, c := newFlags("submit command", d)
	var (
		image   = fs.String("image", "", "required: the image the command runs in: harness (the task's image, where its check and benchmark run, beside the candidate's server on a task with [candidate]) or candidate (the image built from the commit's [candidate] dockerfile)")
		env     = fs.String("env", "", "the environment to run in: host or docker (default: the task's)")
		timeout = fs.Duration("timeout", 0, "the time limit of the command, such as 10m (default: the daemon's); beside a candidate's server, the server's startup time is added")
		service = fs.String("service", "", "a long-lived process to run the command against, split into words as a shell would, quotes kept; not beside a candidate's server")
		probe   = fs.String("probe", "", "a command that succeeds when the service is ready, split as --service is")
		wrap    = fs.String("wrap", "", "an argv prefix, split as --service is: with --image harness on a task with [candidate], put in front of the candidate's server (a profiler's command line, say); otherwise in front of --service when it is given and of the command when not")
		note    = fs.String("note", "", "a note to attach to the job")
		noCache = fs.Bool("no-cache", false, "run a new job even when a matching one exists")
		plan    = fs.Uint("plan", 0, "the number of a plan to link the job to")
		wait    = fs.Bool("wait", false, "block until the job is final, then exit as `wait` does")
	)
	positional, err := parse(fs, flagArgs)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	asJSON := c.json
	switch {
	case len(positional) != 1:
		return fail(d, asJSON, usagef("submit command: give one commit, then -- and the command to run"))
	case !hasCommand || len(argv) == 0:
		return fail(d, asJSON, usagef("submit command: give the command to run after --"))
	case *probe != "" && *service == "":
		return fail(d, asJSON, usagef("--probe: needs --service"))
	case *timeout < 0:
		return fail(d, asJSON, usagef("--timeout: %v is negative", *timeout))
	}
	if err := checkPlanFlags(fs, *plan, false); err != nil {
		return fail(d, asJSON, err)
	}
	if *image == "" {
		return fail(d, asJSON, usagef("submit command: --image is required: harness (the task's image, where its check and benchmark run) or candidate (the image built from the commit's [candidate] dockerfile)"))
	}
	imageKind, err := commandImage(*image)
	if err != nil {
		return fail(d, asJSON, err)
	}
	kind, err := environmentKind(*env)
	if err == nil {
		err = refuseHostPlacement(fs, *env)
	}
	if err == nil && imageKind == cliv1.CommandImage_COMMAND_IMAGE_CANDIDATE && *env == "host" {
		err = usagef("--image candidate: the candidate's image runs in docker, so not with --env host")
	}
	if err != nil {
		return fail(d, asJSON, err)
	}
	prefix, err := splitWords("--wrap", *wrap)
	if err != nil {
		return fail(d, asJSON, err)
	}
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, asJSON, err)
	}
	commit, err := d.commit(ctx, callCtx, positional[0])
	if err != nil {
		return fail(d, asJSON, err)
	}

	// The daemon puts the wrapper where it goes, as only it reads the task: in front of the
	// candidate's server, the service or the command.
	settings := &cliv1.CommandSettings{Argv: argv, Image: imageKind, Wrap: prefix}
	if *timeout > 0 {
		settings.Timeout = durationpb.New(*timeout)
	}
	if *service != "" {
		serviceArgv, err := splitWords("--service", *service)
		if err != nil {
			return fail(d, asJSON, err)
		}
		probeArgv, err := splitWords("--probe", *probe)
		if err != nil {
			return fail(d, asJSON, err)
		}
		if len(serviceArgv) == 0 {
			return fail(d, asJSON, usagef("--service: %q has no command in it", *service))
		}
		settings.Service = &cliv1.ServiceSpec{Argv: serviceArgv, Probe: probeArgv}
	}
	return send(ctx, d, asJSON, *wait, &cliv1.SubmitRequest{
		Context: callCtx,
		Spec: &cliv1.JobSpec{
			Commit: commit, Environment: kind, Placement: c.placement, Allocation: c.alloc,
			Settings: &cliv1.JobSpec_Command{Command: settings},
		},
		NoCache: *noCache, Note: *note, Plan: uint32(*plan),
	})
}

// commandImage reads --image, which names one of the two images a command runs in.
func commandImage(name string) (cliv1.CommandImage, error) {
	switch name {
	case "harness":
		return cliv1.CommandImage_COMMAND_IMAGE_HARNESS, nil
	case "candidate":
		return cliv1.CommandImage_COMMAND_IMAGE_CANDIDATE, nil
	}
	return 0, usagef("--image: %q is neither harness (the task's image, where its check and benchmark run) nor candidate (the image built from the commit's [candidate] dockerfile)", name)
}

// checkPlanFlags refuses a --plan that cannot name a plan, and --plan beside --no-plan: a job tests
// a plan or none. A plan is numbered from 1, so an explicit --plan 0 is a mistake and not "none",
// and a number a plan could never have is refused rather than cut to fit. The daemon makes the same
// refusals of a request that does not come from this command, and the plan's existence is its to
// check.
func checkPlanFlags(fs *flag.FlagSet, plan uint, noPlan bool) error {
	var given bool
	fs.Visit(func(f *flag.Flag) { given = given || f.Name == "plan" })
	switch {
	case plan != 0 && noPlan:
		return usagef("--plan and --no-plan: give one; --plan N links the job to plan N and --no-plan says it tests no plan")
	case given && plan == 0:
		return usagef("--plan: plans are numbered from 1; give the plan's number, or --no-plan for a measure that tests none")
	case plan > math.MaxUint32:
		return usagef("--plan: %d is not a plan number", plan)
	}
	return nil
}

// send submits, prints the job, and with wait blocks until it is final.
func send(ctx context.Context, d deps, asJSON, wait bool, req *cliv1.SubmitRequest) int {
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, asJSON, err)
	}
	defer cl.Close()
	resp, err := cl.Submit(ctx, req)
	if err != nil {
		return fail(d, asJSON, err)
	}
	if asJSON {
		printJSON(d, resp)
	} else {
		fmt.Fprintln(d.stdout, describeSubmitted(resp))
	}
	if !wait {
		return exitOK
	}
	return waitFor(ctx, d, cl, req.GetContext(), []string{resp.GetJob().GetId()}, client.WaitOptions{}, asJSON)
}

// refuseHostPlacement refuses a --placement given on the command line beside --env host: a host job
// runs on this machine and claims no placement, so it could not run where the flag says. A
// placement that only comes from VIBESYS_PLACEMENT is not refused, since it is the default every
// submit inherits for its docker jobs, and the daemon records no placement for a host job.
func refuseHostPlacement(fs *flag.FlagSet, env string) error {
	if env != "host" {
		return nil
	}
	var err error
	fs.Visit(func(f *flag.Flag) {
		if f.Name == "placement" && f.Value.String() != "" {
			err = usagef("--placement: a host job (--env host) runs on this machine and claims no placement; drop --placement or use --env docker")
		}
	})
	return err
}

func environmentKind(name string) (cliv1.EnvironmentKind, error) {
	switch name {
	case "":
		return cliv1.EnvironmentKind_ENVIRONMENT_KIND_UNSPECIFIED, nil
	case "host":
		return cliv1.EnvironmentKind_ENVIRONMENT_KIND_HOST, nil
	case "docker":
		return cliv1.EnvironmentKind_ENVIRONMENT_KIND_DOCKER, nil
	default:
		return 0, usagef("--env: %q is not an environment (host or docker)", name)
	}
}

// connect reaches the daemon, starting one when none runs.
func connect(ctx context.Context, d deps) (*client.Client, error) {
	opts, err := d.options()
	if err != nil {
		return nil, err
	}
	return client.Connect(ctx, opts)
}

// timeoutNote is what a wait that timed out tells its caller, who might otherwise think the jobs
// were stopped.
func timeoutNote(after time.Duration) string {
	return fmt.Sprintf("timed out after %v; the jobs are still going. A timeout never cancels a job.", after)
}
