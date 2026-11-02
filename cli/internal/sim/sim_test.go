package sim_test

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"os"
	"slices"
	"strconv"
	"strings"
	"testing"
	"testing/synctest"

	"google.golang.org/grpc/metadata"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
)

const commit = "0123456789abcdef0123456789abcdef01234567"

// profile is which faults a seed injects, so that a seed can stress one thing.
type profile struct {
	storeWrites, storeReads, supervisorFaults, crashes, machine, unmount, sever bool
	// outage makes the stores fail for a stretch.
	outage bool
	// attempts bounds the daemon's retries. At the eight of a real daemon a random schedule almost
	// never serves a whole run of them, and giving up is a path to explore like any other.
	attempts     int
	faultPercent int
	chaosSteps   int
	jobs         int
	// recovery aims the seed at what a restart finds: little chaos, no faults, and a restart.
	recovery bool
	// probes makes a live supervisor sometimes fail to answer the probe of a restart.
	probes bool
	// hangs makes a store call sometimes not answer at all, as on a hard mount.
	hangs bool
	// lost makes one repository go away for good partway through, and nothing else go wrong but
	// repositories going away, so that all that befalls any job is a repository's doing.
	lost bool
	// lostHangs says the lost repository does not fail its calls but never answers them.
	lostHangs bool
	// forget says the lost repository is given up with `daemon forget`, while the world is in
	// chaos or when it heals.
	forget bool
}

func profileOf(r *rand.Rand) profile {
	coin := func() bool { return r.IntN(2) == 0 }
	if r.IntN(5) == 0 {
		return profile{
			lost: true, lostHangs: coin(), forget: coin(), unmount: true, outage: true, crashes: coin(),
			attempts: 1 + r.IntN(3), chaosSteps: 80 + r.IntN(200), jobs: 2 + r.IntN(4),
		}
	}
	if r.IntN(3) == 0 {
		return profile{
			recovery: true, crashes: true, unmount: coin(), probes: coin(),
			chaosSteps: 15 + r.IntN(50), jobs: 1 + r.IntN(3),
		}
	}
	return profile{
		storeWrites: coin(), storeReads: coin(), supervisorFaults: coin(),
		crashes: coin(), machine: coin(), unmount: coin(), sever: coin(), outage: coin(), hangs: coin(),
		attempts:     1 + r.IntN(4),
		faultPercent: 3 + r.IntN(25),
		chaosSteps:   80 + r.IntN(300),
		jobs:         1 + r.IntN(6),
	}
}

// op is one client call in flight.
type op struct {
	name   string
	cancel context.CancelFunc
	done   chan string
	ended  bool
}

// option is one thing that may happen next.
type option struct {
	label string
	do    func()
}

// faultFor picks how a call goes.
func (w *world) faultFor(c *call, heal bool) verdict {
	store, reads := strings.Contains(c.label, " store"), !c.write
	if store {
		if repo, absent := w.awayIn(c.label); absent {
			if w.profile.lostHangs && repo == w.lost {
				return hang
			}
			return away
		}
	}
	if heal {
		return pass
	}
	if w.down && store {
		return away
	}
	p := w.profile
	allowed := (store && reads && p.storeReads) || (store && !reads && p.storeWrites) ||
		(!store && p.supervisorFaults)
	spawn := strings.Contains(c.label, "sup.Spawn")
	if p.crashes && !p.recovery && w.rng.IntN(100) < p.faultPercent/3 {
		if w.rng.IntN(2) == 0 {
			return crashBefore
		}
		return crashAfter
	}
	if !allowed || w.rng.IntN(100) >= p.faultPercent {
		return pass
	}
	if store && p.hangs && w.rng.IntN(3) == 0 {
		return hang
	}
	if !store && !strings.Contains(c.label, "sup.Follow") && w.rng.IntN(3) == 0 {
		return hang
	}
	if c.write && !spawn && w.rng.IntN(2) == 0 {
		return failAfter
	}
	return failBefore
}

func (w *world) answer(c *call, v verdict) {
	w.gate.remove(c)
	w.tracef("    %s: %s", c.label, []string{"ok", "fails", "takes effect and fails", "dies before", "dies after", "never answers", "is away"}[v])
	c.decide <- v
	switch v {
	case crashBefore:
		w.crash()
	case crashAfter:
		<-c.effected
		w.crash()
	}
}

// start begins a client call as an actor. do returns what the call's end means, or a violation.
func (w *world) start(kind string, do func(ctx context.Context) string) {
	w.opCount++
	name := fmt.Sprintf("%s%d", kind, w.opCount)
	ctx, cancel := context.WithCancel(metadata.AppendToOutgoingContext(w.ctx, actorKey, name))
	o := &op{name: name, cancel: cancel, done: make(chan string, 1)}
	w.ops = append(w.ops, o)
	w.tracef("    %s starts", name)
	w.group.Go(func() { o.done <- do(ctx) })
}

func (w *world) callContext(s int) *cliv1.CallContext {
	return &cliv1.CallContext{Repository: string(scopes[s].Repository()), Task: string(scopes[s].Task()), RunId: "run", Harness: "h", Agent: "a"}
}

func (w *world) submit() {
	w.opCount++
	token := fmt.Sprintf("t%d", len(w.tokens)+1)
	sc := &script{token: token, code: w.rng.IntN(2)}
	lines := 2 + w.rng.IntN(3)
	for i := range lines {
		sc.lines = append(sc.lines, fmt.Sprintf("%s-%d", token, i+1))
		if i < lines-1 {
			sc.gates = append(sc.gates, fmt.Sprintf("%s.%d", token, i+1))
		}
	}
	w.tokens[token] = sc
	w.noteSubmit(token, false)
	s := w.rng.IntN(len(scopes))
	cc := w.callContext(s)
	w.start("submit", func(ctx context.Context) string {
		c, err := client.Connect(ctx, w.options())
		if err != nil {
			w.mu.Lock()
			w.unanswered[token] = true
			w.mu.Unlock()
			return ""
		}
		defer c.Close()
		_, err = c.Submit(ctx, &cliv1.SubmitRequest{Context: cc, Spec: &cliv1.JobSpec{
			Commit: commit, Environment: cliv1.EnvironmentKind_ENVIRONMENT_KIND_HOST,
			Settings: &cliv1.JobSpec_Command{Command: &cliv1.CommandSettings{Argv: sc.argv()}},
		}})
		if err != nil {
			w.mu.Lock()
			w.unanswered[token] = true
			w.mu.Unlock()
		}
		w.noteSubmit(token, true)
		return ""
	})
}

func (w *world) scopeIndex(l located) int {
	return slices.Index(scopes, l.scope)
}

func (w *world) cancel(l located) {
	w.asked[w.refIn(l.scope, l.job.ID)] = true
	cc, id := w.callContext(w.scopeIndex(l)), string(l.job.ID)
	w.start("cancel", func(ctx context.Context) string {
		c, err := client.Connect(ctx, w.options())
		if err != nil {
			return ""
		}
		defer c.Close()
		_, _ = c.Cancel(ctx, &cliv1.CancelRequest{Context: cc, JobId: id, Reason: "asked"})
		return ""
	})
}

// wait is one `wait`. A wait reports how the jobs ended, or that it could not tell; a stream that
// broke never reads as the job's end.
func (w *world) wait(l located) {
	cc, id := w.callContext(w.scopeIndex(l)), string(l.job.ID)
	scope := l.scope
	w.start("wait", func(ctx context.Context) string {
		c, err := client.Connect(ctx, w.options())
		if err != nil {
			return ""
		}
		defer c.Close()
		r, err := c.Wait(ctx, cc, []string{id}, client.WaitOptions{})
		if ctx.Err() != nil {
			return ""
		}
		if err != nil {
			if errors.Is(err, client.ErrEndUnknown) {
				return ""
			}
			return fmt.Sprintf("wait for %s/%s ended with %v", string(scope.Task()), id, err)
		}
		kind := r.Outcome.GetKind()
		if kind != cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_ALL_SUCCEEDED && kind != cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_SOME_FAILED {
			return fmt.Sprintf("wait for all of %s ended %v", id, kind)
		}
		j, jerr := w.raw[scope].Job(context.Background(), core.JobID(id))
		if jerr != nil || !j.State.Final() {
			return fmt.Sprintf("wait for %s said %v, the store has %s", id, kind, j.State)
		}
		if succeeded := kind == cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_ALL_SUCCEEDED; succeeded != (j.State == core.JobSucceeded) {
			return fmt.Sprintf("wait for %s said %v, the store has %s", id, kind, j.State)
		}
		if got := r.Outcome.GetJobs(); len(got) != 1 || got[0].GetId() != id {
			return fmt.Sprintf("wait for %s sent the jobs %v", id, got)
		}
		return ""
	})
}

// forgetAttempts bounds an operator's patience, so that a daemon that keeps refusing to come up
// ends in the world's own check and not in a loop no time passes in.
const forgetAttempts = 20

// forget gives up a repository that is gone, as an operator would when the world heals: again until
// it is done, as a daemon may die under the call or a store may not answer in time. It must not be
// refused, as the repository is gone. A repository already forgotten is not known.
func (w *world) forget(repo string) {
	w.mu.Lock()
	w.forgetting = true
	w.mu.Unlock()
	w.start("forget", func(ctx context.Context) string {
		for range forgetAttempts {
			if ctx.Err() != nil {
				break
			}
			if done, violation := w.tryForget(ctx, repo); done || violation != "" {
				return violation
			}
		}
		return ""
	})
}

// forgetOnce is one try at giving up the lost repository while the world is in chaos: its command
// may still run, and the other repository's work waits. Another try is offered until one is done.
func (w *world) forgetOnce(repo string) {
	w.mu.Lock()
	w.forgetting, w.trying = true, true
	w.mu.Unlock()
	w.start("forget", func(ctx context.Context) string {
		defer func() {
			w.mu.Lock()
			w.trying = false
			w.mu.Unlock()
		}()
		_, violation := w.tryForget(ctx, repo)
		return violation
	})
}

// mayForget says a try at giving up the lost repository can start: none is in flight or went
// through.
func (w *world) mayForget() bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.lost != "" && !w.forgot && !w.trying
}

// tryForget asks a daemon to give up the repository once, and says whether that is done, or what
// it did wrong.
func (w *world) tryForget(ctx context.Context, repo string) (bool, string) {
	c, err := client.Connect(ctx, w.options())
	if err != nil {
		return false, ""
	}
	_, err = c.ForgetRepository(ctx, repo)
	c.Close()
	var f *core.Failure
	switch {
	case err == nil, errors.As(err, &f) && f.Kind == core.FailureNotFound:
		w.mu.Lock()
		w.forgot = true
		w.mu.Unlock()
		return true, ""
	case f != nil && f.Kind == core.FailureConflict:
		return false, fmt.Sprintf("forgetting %s was refused: %v", repo, err)
	}
	return false, ""
}

// forgetAway forgets a repository whose disk does not answer, as an operator who took it for gone
// would. The daemon cannot tell it is gone, so it must refuse: the repository may be whole, with
// jobs that run.
func (w *world) forgetAway(repo string) {
	w.start("forget-away", func(ctx context.Context) string {
		c, err := client.Connect(ctx, w.options())
		if err != nil {
			return ""
		}
		defer c.Close()
		if _, err := c.ForgetRepository(ctx, repo); err == nil {
			return fmt.Sprintf("forgot %s, which was only away", repo)
		}
		return ""
	})
}

// awayNow is a repository that is away now and not lost, if there is one.
func (w *world) awayNow() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, s := range scopes {
		if w.absent[s.Repository] && s.Repository != w.lost {
			return s.Repository
		}
	}
	return ""
}

// finalWait is what the check after the chaos runs: every job, through a client, to its end.
func (w *world) finalWait(l located) { w.wait(l) }

// actions are the things the world does on its own.
func (w *world) actions() []option {
	var out []option
	add := func(label string, do func()) { out = append(out, option{label, do}) }
	all := w.jobs()
	if len(w.tokens) < w.profile.jobs {
		add("submit", w.submit)
	}
	if len(all) > 0 {
		l := all[w.rng.IntN(len(all))]
		name := fmt.Sprintf("%s/%s", string(l.scope.Task()), l.job.ID)
		add("cancel "+name, func() { w.cancel(l) })
		add("wait "+name, func() { w.wait(l) })
	}
	w.mu.Lock()
	up := w.daemon != nil
	follows := sortedKeys(mapKeysToString(w.follows))
	w.mu.Unlock()
	p := w.profile
	// A profile of repositories going away lets work get going first, as a repository that is away
	// from the start only refuses what it is sent.
	outages := !p.lost || slices.ContainsFunc(all, func(l located) bool { return l.job.State == core.JobRunning })
	if up && p.crashes && !p.recovery {
		add("crash", w.crash)
		add("restart", func() {
			w.start("restart", func(ctx context.Context) string {
				c, err := client.Restart(ctx, w.options())
				if err == nil {
					c.Close()
				}
				return ""
			})
		})
	}
	if p.machine && len(w.spawned) > 0 {
		add("reboot", func() {
			w.crash()
			for _, r := range w.spawned {
				w.killed[r] = true
			}
			w.survivors.Reboot()
		})
		r := w.spawned[w.rng.IntN(len(w.spawned))]
		add("kill "+r.String(), func() {
			if w.survivors.Kill(r) {
				w.killed[r] = true
			}
		})
		add("the place of "+r.String()+" is gone for good", func() {
			if w.survivors.Vanish(r) {
				w.mu.Lock()
				w.killed[r], w.vanished[r] = true, true
				w.mu.Unlock()
			}
		})
	}
	if p.outage && outages {
		add("flip the stores", func() { w.down = !w.down })
	}
	if p.unmount && outages {
		repo := string(scopes[w.rng.IntN(len(scopes))].Repository())
		add("flip "+repo, func() {
			w.mu.Lock()
			defer w.mu.Unlock()
			if repo != w.lost {
				w.absent[repo] = !w.absent[repo]
			}
		})
		add("every repository goes away", func() {
			w.mu.Lock()
			defer w.mu.Unlock()
			for _, s := range scopes {
				w.absent[string(s.Repository())] = true
			}
		})
		if away := w.awayNow(); !p.lost && away != "" {
			add("forget "+away+", which is only away", func() { w.forgetAway(away) })
		}
		add("every repository comes back", func() {
			w.mu.Lock()
			defer w.mu.Unlock()
			clear(w.absent)
			if w.lost != "" {
				w.absent[w.lost] = true
			}
		})
	}
	if p.lost && w.lost == "" {
		// It strikes a repository with a job running, and with work of another repository waiting,
		// and the daemon goes with it, which is what leaves only the supervisor to say what runs.
		for _, repo := range w.struck(all) {
			add(repo+" is gone for good", func() {
				w.mu.Lock()
				w.lost, w.absent[repo] = repo, true
				w.mu.Unlock()
				if up && p.crashes {
					w.crash()
				}
			})
		}
	}
	if p.forget && w.mayForget() {
		// Given up while its command may still run and the other repository's work waits: the
		// command ends before that work starts, or holds the machine while it can run.
		add("forget "+w.lost, func() { w.forgetOnce(w.lost) })
	}
	if p.sever {
		for _, ref := range follows {
			add("sever follow "+ref, func() {
				w.mu.Lock()
				defer w.mu.Unlock()
				for r, sever := range w.follows {
					if r.String() == ref {
						sever(errInjected)
					}
				}
			})
		}
		w.mu.Lock()
		conns := len(w.conns)
		w.mu.Unlock()
		if conns > 0 {
			add("sever clients", func() {
				w.mu.Lock()
				defer w.mu.Unlock()
				for _, c := range w.conns {
					c.Close()
				}
				w.conns = nil
			})
		}
	}
	return out
}

func mapKeysToString[V any](m map[lifecycle.Ref]V) map[string]V {
	out := make(map[string]V, len(m))
	for k, v := range m {
		out[k.String()] = v
	}
	return out
}

// moves are what the driver may let go on.
func (w *world) moves(heal bool) []option {
	var out []option
	for _, c := range w.gate.sorted() {
		out = append(out, option{"call " + c.label, func() { w.answer(c, w.faultFor(c, heal)) }})
	}
	for _, name := range w.gates.names() {
		out = append(out, option{"command passes " + name, func() { w.gates.release(name) }})
	}
	for _, name := range w.startLock.names() {
		out = append(out, option{"start lock to " + name, func() { w.startLock.grant(name) }})
	}
	// Once the world is healed a call answers before any time goes by, so no limit falls on one
	// that is merely waiting its turn. A store that hangs has answered with that, and takes time.
	// Nothing but repositories goes wrong in a profile of them, and a late call is not one.
	calls := len(w.gate.sorted())
	if at, ok := w.timers.next(); ok && (!heal || len(out) == 0) && (!w.profile.lost || calls == 0) {
		out = append(out, option{fmt.Sprintf("time moves to %s", at), func() { w.timers.advance(at) }})
	}
	if heal {
		for _, scope := range w.toGiveUp() {
			out = append(out, option{"the operator removes the owner record of " + scope.Repository + " " + scope.Task, func() { w.giveUp(scope) }})
		}
	}
	return out
}

// toGiveUp is the stores whose owner record an operator removes, as the daemon tells them to: a job
// of each runs in it, whose place is gone for good, and was not given up yet.
func (w *world) toGiveUp() []app.Scope {
	var out []app.Scope
	for _, l := range w.jobs() {
		ref := w.refIn(l.scope, l.job.ID)
		w.mu.Lock()
		due := l.job.State == core.JobRunning && w.vanished[ref] && !slices.Contains(w.givenUp[l.scope], l.job.ID)
		w.mu.Unlock()
		if due && !slices.Contains(out, l.scope) {
			out = append(out, l.scope)
		}
	}
	return out
}

// giveUp is the next owner of a store finding its record removed: every job it has running is
// given up.
func (w *world) giveUp(scope app.Scope) {
	for _, l := range w.jobs() {
		w.mu.Lock()
		if l.scope == scope && l.job.State == core.JobRunning && !slices.Contains(w.givenUp[scope], l.job.ID) {
			w.givenUp[scope] = append(w.givenUp[scope], l.job.ID)
		}
		w.mu.Unlock()
	}
}

// check says what must hold after every step.
func (w *world) check() {
	running := map[core.Scope]int{}
	for _, l := range w.jobs() {
		ref := w.refIn(l.scope, l.job.ID)
		if l.job.State == core.JobRunning {
			running[l.scope]++
		}
		if was, ok := w.finals[ref]; ok && was != l.job.State {
			w.violate("job %s was final as %s and is now %s", ref, was, l.job.State)
		}
		if l.job.State.Final() {
			w.finals[ref] = l.job.State
			w.checkEnd(ref, l)
		}
	}
	// The machine runs one command at a time. A store can show more than the machine does, as a
	// store that was away shows a job whose supervisor died meanwhile, but not more than one.
	for _, scope := range scopes {
		if running[scope] > 1 {
			w.violate("%d jobs of %s are running", running[scope], string(scope.Task()))
		}
	}
	if n := w.env.Active(); n > 1 {
		w.violate("%d commands run at once", n)
	}
	starts := w.startsOf()
	for _, token := range sortedKeys(w.tokens) {
		if n := countOf(starts, token); n > 1 {
			w.violate("command %s ran %d times", token, n)
		}
	}
	for ref, n := range w.appends {
		if n > 2 {
			w.violate("job %s appended %d times with no time passing", ref, n)
		}
	}
	for i := range w.ops {
		o := w.ops[i]
		if o.ended {
			continue
		}
		select {
		case msg := <-o.done:
			o.ended = true
			o.cancel()
			w.tracef("    %s ended", o.name)
			if msg != "" {
				w.violate("%s", msg)
			}
		default:
		}
	}
}

// checkEnd checks a job's end against what its command did.
func (w *world) checkEnd(ref lifecycle.Ref, l located) {
	sc := w.tokens[tokenOf(l.job)]
	if sc == nil {
		w.violate("job %s has an unknown command", ref)
		return
	}
	out := w.outputOf(l)
	var own []string
	said := false
	for _, line := range strings.Split(strings.TrimSuffix(out, "\n"), "\n") {
		if strings.HasPrefix(line, "vibesys-cli:") {
			said = true
			continue
		}
		own = append(own, line)
	}
	text := strings.Join(own, "\n")
	if text != "" {
		text += "\n"
	}
	if !strings.HasPrefix(sc.output(), text) {
		w.violate("job %s has output %q, which is not a prefix of %q", ref, out, sc.output())
	}
	switch l.job.State {
	case core.JobSucceeded:
		if sc.code != 0 || out != sc.output() {
			w.violate("job %s succeeded with output %q and script exit %d", ref, out, sc.code)
		}
	case core.JobFailed:
		if l.job.ExitCode == nil && !said {
			w.violate("job %s failed with no exit code and no line saying why", ref)
		}
		if l.job.ExitCode == nil && w.profile.lost {
			w.violate("job %s failed for what happened to a repository: %q", ref, out)
		}
		if l.job.ExitCode != nil && (*l.job.ExitCode != sc.code || out != sc.output()) {
			w.violate("job %s failed with code %d and output %q", ref, *l.job.ExitCode, out)
		}
	case core.JobLost:
		if !w.killed[ref] {
			w.violate("job %s is lost though no supervisor of it was killed", ref)
		}
	case core.JobCancelled:
		if !w.asked[ref] {
			w.violate("job %s is cancelled though nobody asked", ref)
		}
	}
}

func countOf(list []string, s string) int {
	n := 0
	for _, x := range list {
		if x == s {
			n++
		}
	}
	return n
}

// finalCheck says what must hold when the world has healed.
func (w *world) finalCheck() {
	w.mu.Lock()
	forgot := w.forgot
	w.mu.Unlock()
	// A repository that is gone for good says so, and so the operator's forget goes through; one
	// that never answers cannot be told gone.
	if w.profile.forget && w.lost != "" && !w.profile.lostHangs && !forgot {
		w.violate("%s is gone for good and was not forgotten", w.lost)
	}
	all := w.jobs()
	byToken := map[string]located{}
	for _, l := range all {
		byToken[tokenOf(l.job)] = l
	}
	for _, s := range scopes {
		var order []string
		for _, token := range w.startsOf() {
			if l, ok := byToken[token]; ok && l.scope == s && !w.unanswered[token] {
				order = append(order, token)
			}
		}
		// Submissions that overlap have no order. One that was answered before the other began
		// does.
		for i, later := range order {
			for _, first := range order[i+1:] {
				if w.submitEnd[first] != 0 && w.submitEnd[first] < w.submitBegin[later] {
					w.violate("%s started before %s, which was submitted first", later, first)
				}
			}
		}
	}
}

// struck is the repositories with a job running while another repository has one queued.
func (w *world) struck(all []located) []string {
	var out []string
	for _, l := range all {
		queued := slices.ContainsFunc(all, func(o located) bool {
			return o.job.State == core.JobQueued && string(o.scope.Repository()) != string(l.scope.Repository())
		})
		if l.job.State == core.JobRunning && queued && !slices.Contains(out, string(l.scope.Repository())) {
			out = append(out, string(l.scope.Repository()))
		}
	}
	return out
}

// settled says everything ended: every job, except the jobs of a repository that is gone, whose
// waiters were told why. No fault of that repository keeps the rest from ending.
func (w *world) settled() bool {
	for _, l := range w.jobs() {
		if !l.job.State.Final() && string(l.scope.Repository()) != w.lost {
			return false
		}
	}
	for _, o := range w.ops {
		if !o.ended {
			return false
		}
	}
	return true
}

// teardown ends everything the simulation started, so the bubble ends clean.
func (w *world) teardown() {
	w.crash()
	w.stop()
	for _, o := range w.ops {
		o.cancel()
	}
	w.group.Wait()
	w.survivors.Close()
	synctest.Wait()
}

// launchRecording says the move records a launch: a crash here leaves a queued job with a supervisor.
func launchRecording(o option) bool { return strings.Contains(o.label, "queued>running") }

// lateRestart restarts the daemon while the world is still broken: a repository can be away, and a
// cancel can arrive while the new daemon clears what the old one left. Faults stop here, so what
// happens is recovery's own doing.
func (w *world) lateRestart() {
	if !w.profile.recovery && (!w.profile.crashes || w.rng.IntN(2) == 0) {
		return
	}
	w.tracef("late restart")
	w.crash()
	if w.profile.unmount {
		repo := string(scopes[w.rng.IntN(len(scopes))].Repository())
		w.tracef("%s goes away", repo)
		w.mu.Lock()
		w.absent[repo] = true
		w.mu.Unlock()
	}
	w.start("hello", func(ctx context.Context) string {
		c, err := client.Connect(ctx, w.options())
		if err == nil {
			c.Close()
		}
		return ""
	})
	hello := w.ops[len(w.ops)-1]
	after := 0
	for step := 0; step < healBound && after < 40; step++ {
		synctest.Wait()
		w.check()
		if w.violation != "" {
			return
		}
		if hello.ended {
			after++
		}
		pool := w.moves(true)
		pool = append(pool, pool...)
		if all := w.jobs(); len(all) > 0 && w.rng.IntN(4) == 0 {
			l := all[w.rng.IntN(len(all))]
			pool = append(pool, option{"cancel " + string(l.job.ID), func() { w.cancel(l) }})
		}
		if len(pool) == 0 {
			continue
		}
		o := pool[w.rng.IntN(len(pool))]
		w.tracef("L%d %s", step, o.label)
		o.do()
	}
}

// healBound is far more steps than a healed world takes. It turns a daemon that makes no progress
// into a failure.
const healBound = 3000

// simulate runs one seed and returns the violation, if any, and the trace.
func simulate(seed uint64) (string, string) {
	w := newWorld(seed)
	w.tracef("profile %+v", w.profile)
	defer w.teardown()
	for step := range w.profile.chaosSteps {
		synctest.Wait()
		w.check()
		if w.violation != "" {
			return w.violation, w.traced()
		}
		moves := w.moves(false)
		if w.profile.recovery && slices.ContainsFunc(moves, launchRecording) && w.rng.IntN(2) == 0 {
			break
		}
		acts := w.actions()
		pool := append(append(append(append([]option{}, moves...), moves...), moves...), acts...)
		if len(pool) == 0 {
			continue
		}
		o := pool[w.rng.IntN(len(pool))]
		w.tracef("%d %s", step, o.label)
		o.do()
	}
	synctest.Wait()
	w.check()
	if w.violation == "" {
		w.lateRestart()
	}
	if w.violation != "" {
		return w.violation, w.traced()
	}
	// Heal: no more faults, and everything reachable comes back. Every job gets a final wait.
	w.tracef("heal")
	synctest.Wait()
	w.check()
	w.mu.Lock()
	w.down = false
	clear(w.absent)
	if w.lost != "" {
		w.absent[w.lost] = true
	}
	w.mu.Unlock()
	for _, l := range w.jobs() {
		w.finalWait(l)
	}
	if w.profile.forget && w.lost != "" {
		w.forget(w.lost)
	}
	if len(w.jobs()) == 0 {
		w.start("hello", func(ctx context.Context) string {
			c, err := client.Connect(ctx, w.options())
			if err == nil {
				c.Close()
			}
			return ""
		})
	}
	for step := range healBound {
		synctest.Wait()
		w.check()
		if w.violation != "" {
			return w.violation, w.traced()
		}
		if w.settled() {
			w.finalCheck()
			return w.violation, w.traced()
		}
		moves := w.moves(true)
		if len(moves) == 0 {
			w.violate("stuck: nothing can happen and work is left: %s", w.unfinished())
			return w.violation, w.traced()
		}
		o := moves[w.rng.IntN(len(moves))]
		w.tracef("h%d %s", step, o.label)
		o.do()
	}
	w.violate("no progress in %d steps: %s", healBound, w.unfinished())
	return w.violation, w.traced()
}

func (w *world) unfinished() string {
	var out []string
	for _, l := range w.jobs() {
		if !l.job.State.Final() {
			out = append(out, fmt.Sprintf("job %s/%s %s", string(l.scope.Task()), l.job.ID, l.job.State))
		}
	}
	for _, o := range w.ops {
		if !o.ended {
			out = append(out, o.name)
		}
	}
	return strings.Join(out, ", ")
}

func seedRange() (uint64, uint64) {
	first, count := uint64(1), uint64(defaultSeeds)
	if v, err := strconv.ParseUint(os.Getenv("VIBESYS_SIM_SEED"), 10, 64); err == nil {
		first, count = v, 1
	}
	if v, err := strconv.ParseUint(os.Getenv("VIBESYS_SIM_SEEDS"), 10, 64); err == nil {
		count = v
	}
	return first, count
}

const defaultSeeds = 300

func TestSeedsHoldTheInvariants(t *testing.T) {
	first, count := seedRange()
	verbose := os.Getenv("VIBESYS_SIM_TRACE") != ""
	for seed := first; seed < first+count; seed++ {
		var violation, trace string
		synctest.Test(t, func(*testing.T) { violation, trace = simulate(seed) })
		if violation != "" {
			if verbose {
				t.Log(trace)
			}
			t.Errorf("seed %d: %s", seed, violation)
		}
	}
}

func TestASeedReplaysExactly(t *testing.T) {
	first, _ := seedRange()
	for seed := first; seed < first+20; seed++ {
		var a, b string
		synctest.Test(t, func(*testing.T) { _, a = simulate(seed) })
		synctest.Test(t, func(*testing.T) { _, b = simulate(seed) })
		if a != b {
			i := 0
			for i < len(a) && i < len(b) && a[i] == b[i] {
				i++
			}
			lo := max(0, i-300)
			t.Errorf("seed %d replays differently at byte %d:\n--- first\n%s\n--- second\n%s", seed, i, a[lo:min(len(a), i+200)], b[lo:min(len(b), i+200)])
		}
	}
}
