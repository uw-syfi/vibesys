package clientcli

import (
	"context"
	"errors"
	"fmt"

	"github.com/uw-syfi/vibesys/orchestrator/gatewayroot"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
	"github.com/uw-syfi/vibesys/orchestrator/hostlock"
)

// Finding is what a client finds at a root's socket. The set is closed: Decide has an outcome for
// every value, and a new way for a host to be there is a new value here, not a new branch elsewhere.
type Finding int

const (
	// FoundNothing is no host: nobody serves the socket, and nothing on another machine holds the root.
	FoundNothing Finding = iota
	// FoundElsewhere is a host of this root on another machine, seen through a shared disk. It can be
	// neither reached nor signalled from here, and no host can start here while it holds the root.
	FoundElsewhere
	// FoundSilent is a host that was reached and does not answer HostControlService.
	FoundSilent
	// FoundHeld is a host that answers and does not serve the orchestrator, because another process
	// holds its state. A new host would be refused the same way.
	FoundHeld
	// FoundHolderGone is a host that answers and does not serve the orchestrator, because another
	// process held its state when it started and has since gone. A new host can serve.
	FoundHolderGone
	// FoundStateRefused is a host that answers and could not open its state.
	FoundStateRefused
	// FoundServing is a host that serves the orchestrator.
	FoundServing
)

// Findings is every Finding, for what must cover them all.
var Findings = []Finding{FoundNothing, FoundElsewhere, FoundSilent, FoundHeld, FoundHolderGone, FoundStateRefused, FoundServing}

// answers says the host said what it is, so what it said may be read.
func (f Finding) answers() bool {
	return f == FoundHeld || f == FoundHolderGone || f == FoundStateRefused || f == FoundServing
}

// Relation is how the host that answered compares with the host this client would start.
type Relation int

const (
	// RelationSame is the same program as far as anyone can tell: the same release, or the same build.
	RelationSame Relation = iota
	// RelationHostOlder is an older release, which a newer client may replace.
	RelationHostOlder
	// RelationHostNewer is a newer release, which no client replaces unasked.
	RelationHostNewer
	// RelationUnordered is another program that cannot be ordered: either side is a build of no release.
	RelationUnordered
)

// Relations is every Relation.
var Relations = []Relation{RelationSame, RelationHostOlder, RelationHostNewer, RelationUnordered}

// ConfigState is how the configuration now in the root stands against what the host started with.
type ConfigState int

const (
	// ConfigMatches is the configuration the host started with.
	ConfigMatches ConfigState = iota
	// ConfigDiffers is another configuration than the host started with.
	ConfigDiffers
	// ConfigUnreadable is a file that does not read, so no host can start until it is fixed. A host
	// that is running keeps what it started with.
	ConfigUnreadable
)

// ConfigStates is every ConfigState.
var ConfigStates = []ConfigState{ConfigMatches, ConfigDiffers, ConfigUnreadable}

// Verb is what a verb may do to the host. Only VerbStart and VerbRestart may ever replace one.
type Verb int

const (
	// VerbRead reads and steers what exists: every run verb but start. It never stops a host, and
	// starts one only when there is none, since the state it wants is only reached through a host.
	VerbRead Verb = iota
	// VerbStart starts a run, which should run on this release's host and configuration.
	VerbStart
	// VerbRestart is the operator asking for this client's host in place of the one there.
	VerbRestart
	// VerbStop is the operator asking for no host.
	VerbStop
)

// Verbs is every Verb.
var Verbs = []Verb{VerbRead, VerbStart, VerbRestart, VerbStop}

// Action is what a client does about what it found.
type Action int

const (
	// ActionUse uses the host that is there.
	ActionUse Action = iota
	// ActionStart starts a host, for there is none.
	ActionStart
	// ActionReplace proves a new host can start, asks this one to stop only if it has no live run, and
	// starts the new one. Nothing has been touched when the proof fails.
	ActionReplace
	// ActionStop asks the host to stop.
	ActionStop
	// ActionDone ends the verb with nothing to do: it asked for no host, and there is none.
	ActionDone
	// ActionRefuse ends the verb with the host left as it is, because it is in use or newer.
	ActionRefuse
	// ActionUnavailable ends the verb with the host left as it is, because no host here can serve.
	ActionUnavailable
)

// Reading is everything a client knows about a root's host, in the terms the policy decides by. Relation
// and Config mean something only of a host that answered, and are zero otherwise, as are the counts.
type Reading struct {
	Found    Finding
	Relation Relation
	Config   ConfigState
	LiveRuns uint32
	Clients  uint32
}

// Decision is the one outcome for a Reading and a verb, and the exit code when the verb ends there. A
// verb that goes on (use, start, replace, stop) exits with its own code; the decision's says only that
// nothing here ended it.
type Decision struct {
	Action Action
	Exit   int
}

var (
	use         = Decision{ActionUse, ExitOK}
	startOne    = Decision{ActionStart, ExitOK}
	replace     = Decision{ActionReplace, ExitOK}
	stopIt      = Decision{ActionStop, ExitOK}
	done        = Decision{ActionDone, ExitOK}
	refuse      = Decision{ActionRefuse, ExitFailed}
	unreachable = Decision{ActionUnavailable, ExitUnavailable}
)

// Decide is the client's policy. It never stops a host for a read, never replaces a host with live
// runs, and never replaces unasked a host that has other clients or is newer than this client.
func Decide(r Reading, verb Verb) Decision {
	switch r.Found {
	case FoundElsewhere, FoundSilent:
		return unreachable
	case FoundNothing:
		if verb == VerbStop {
			return done
		}
		return startOne
	}
	switch verb {
	case VerbStop:
		return stopIt
	case VerbRestart:
		return decideRestart(r)
	case VerbStart:
		return decideStart(r)
	}
	if r.Found == FoundServing {
		return use
	}
	return unreachable
}

// decideRestart is `host restart`, which is how an operator overrules everything but live runs and a
// holder that would refuse the new host just the same.
func decideRestart(r Reading) Decision {
	switch {
	case r.Found == FoundHeld:
		return unreachable
	case r.LiveRuns > 0:
		return refuse
	}
	return replace
}

// decideStart is `run start`: replace a host that is not this client's only when nothing depends on it.
func decideStart(r Reading) Decision {
	occupied := r.LiveRuns > 0 || r.Clients > 0
	switch r.Found {
	case FoundHeld:
		return unreachable
	case FoundHolderGone, FoundStateRefused:
		if occupied || r.Relation == RelationHostNewer || r.Config == ConfigUnreadable {
			return refuse
		}
		return replace
	}
	switch {
	case r.Relation == RelationHostNewer:
		if r.Config == ConfigDiffers {
			return refuse
		}
		return use
	case r.Relation == RelationSame && r.Config != ConfigDiffers:
		return use
	case occupied, r.Config == ConfigUnreadable:
		return refuse
	}
	return replace
}

// Evidence is what a client gathers about a root's host. Identity is what the host said, or nil when
// none answered; then Silent says one was reached, and Holder is the record of whoever holds the
// root's lock, if anyone does.
type Evidence struct {
	Identity *pb.IdentifyResponse
	Silent   bool
	Holder   *pb.HostLock
	// Machine is the name of the machine the client is on.
	Machine string
	// ClientRelease is this client's release.
	ClientRelease string
	// Build is the identity of the host this client would start, or empty when it cannot tell.
	Build string
	// ConfigDigest is the digest of the configuration now in the root, unless ConfigUnreadable.
	ConfigDigest     string
	ConfigUnreadable bool
}

// Classify reads the evidence into the terms of the policy.
func Classify(e Evidence) Reading {
	switch {
	case e.Silent:
		return Reading{Found: FoundSilent}
	case e.Identity == nil && e.Holder != nil && hostlock.Elsewhere(e.Holder, e.Machine):
		return Reading{Found: FoundElsewhere}
	case e.Identity == nil:
		return Reading{Found: FoundNothing}
	}
	r := Reading{
		Found: serving(e.Identity), Relation: relate(e.Identity, e.ClientRelease, e.Build), Config: ConfigMatches,
		LiveRuns: e.Identity.GetLiveRuns(), Clients: e.Identity.GetConnectedClients(),
	}
	switch {
	case e.ConfigUnreadable:
		r.Config = ConfigUnreadable
	case e.Identity.GetConfigDigest() != e.ConfigDigest:
		r.Config = ConfigDiffers
	}
	return r
}

func serving(id *pb.IdentifyResponse) Finding {
	unserved := id.GetUnserved()
	switch {
	case unserved == nil:
		return FoundServing
	case unserved.GetReason() == pb.UnservedReason_UNSERVED_REASON_HELD_ELSEWHERE:
		return FoundHeld
	case unserved.GetReason() == pb.UnservedReason_UNSERVED_REASON_HOLDER_GONE:
		return FoundHolderGone
	}
	return FoundStateRefused
}

// relate says how the host compares. The host orders two releases, once, for every client. A build of
// no release cannot be ordered, and is the same only when it is the very program this client would
// start: the name "dev" says nothing about which build it is.
func relate(id *pb.IdentifyResponse, clientRelease, build string) Relation {
	switch id.GetRelation() {
	case pb.ReleaseRelation_RELEASE_RELATION_SAME:
		return RelationSame
	case pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER:
		return RelationHostOlder
	case pb.ReleaseRelation_RELEASE_RELATION_HOST_NEWER:
		return RelationHostNewer
	}
	if id.GetRelease() == clientRelease && id.GetBuildId() != "" && id.GetBuildId() == build {
		return RelationSame
	}
	return RelationUnordered
}

// StartRefused is a host that was started and will not serve. Detail is all of why, from this
// attempt alone, and nothing is left running.
type StartRefused struct{ Detail string }

func (e *StartRefused) Error() string { return "the host refused to start: " + e.Detail }

// StartTimedOut is a host that was started and did not come up in time. It was ended.
type StartTimedOut struct{ Err error }

func (e *StartTimedOut) Error() string {
	return "the host did not come up in time: " + e.Err.Error()
}

func (e *StartTimedOut) Unwrap() error { return e.Err }

// CannotStart is what Connect and Prove say of a host that would not come up, found before anything
// was started or stopped. Problem names what is wrong and how to put it right.
type CannotStart struct{ Problem string }

func (e *CannotStart) Error() string { return "no host can start: " + e.Problem }

// placeFailure is the failure of a root that could not be worked out, or of a host that cannot start
// there.
func placeFailure(err error) error {
	var place *gatewayroot.Error
	var cannot *CannotStart
	switch {
	case errors.As(err, &place):
		return &failure{code: ExitUsage, msg: err.Error()}
	case errors.As(err, &cannot):
		return failed("%v", err)
	}
	return unavailable("%v", err)
}

// startFailure is the failure of a dial that may have started a host.
func startFailure(err error) error {
	var refused *StartRefused
	var late *StartTimedOut
	var cannot *CannotStart
	switch {
	case errors.As(err, &refused), errors.As(err, &cannot):
		return failed("%v", err)
	case errors.As(err, &late):
		return timedOut("%v", err)
	}
	return unavailable("start the host: %v", err)
}

// startThrough makes sure a host serves, and that it is one that came up, before anything else is
// asked of it: a typed refusal would not survive the layers a later call dials through.
func startThrough(ctx context.Context, dial Dial) error {
	conn, err := dial(ctx)
	if err != nil {
		return startFailure(err)
	}
	return conn.Close()
}

// sight is one look at a root's host: what the policy reads, and what was seen, for saying so.
type sight struct {
	Reading
	id *pb.IdentifyResponse
	// silence is the failure of a host that does not answer.
	silence error
	// holder is the record of the root's lock, for a host on another machine.
	holder *pb.HostLock
	// config is why the configuration in the root does not read.
	config error
}

// look finds out what is at the root, and changes nothing. It fails only for a root it cannot work out.
func (c *call) look(ctx context.Context) (sight, error) {
	id, err := c.identify(ctx)
	evidence := Evidence{Identity: id, ClientRelease: c.Version, Machine: c.machine()}
	seen := sight{id: id}
	switch {
	case errors.Is(err, ErrNoHost):
		if c.Hosts != nil {
			// A record that does not read names no machine, and is for `host stop --force` to report.
			if holder, err := c.Hosts.Holder(c.root); err == nil {
				evidence.Holder, seen.holder = holder, holder
			}
		}
	case exitCodeOf(err) == ExitUsage && err != nil:
		return seen, err
	case err != nil:
		evidence.Silent, seen.silence = true, err
	default:
		config, err := c.Configs.Load(c.root)
		if err != nil {
			evidence.ConfigUnreadable, seen.config = true, err
		} else {
			evidence.ConfigDigest = hostconfig.Digest(config)
		}
		if id.GetRelation() != pb.ReleaseRelation_RELEASE_RELATION_SAME && id.GetRelease() == c.Version && c.HostBuild != nil {
			evidence.Build = c.HostBuild(c.root)
		}
	}
	seen.Reading = Classify(evidence)
	return seen, nil
}

func (c *call) machine() string {
	if c.Hosts == nil {
		return ""
	}
	return c.Hosts.Machine()
}

// lookPast is look for a verb the host's other clients would refuse. A connection that just closed, a
// client's own last one for instance, is counted until the host has seen it close, so a host that
// reports only connections is asked again a few times before they are believed.
func (c *call) lookPast(ctx context.Context, verb Verb) (sight, error) {
	seen, err := c.look(ctx)
	for range clientLooks {
		if err != nil || seen.LiveRuns > 0 || seen.Clients == 0 || Decide(seen.Reading, verb).Action != ActionRefuse {
			break
		}
		select {
		case <-ctx.Done():
			return seen, nil
		case <-c.Clock.After(clientLookEvery):
		}
		seen, err = c.look(ctx)
	}
	return seen, err
}

// settle looks at the host, does what Decide says, and returns how to reach the host for the verb.
func (c *call) settle(ctx context.Context, verb Verb) (Dial, error) {
	seen, err := c.lookPast(ctx, verb)
	if err != nil {
		return nil, err
	}
	switch decision := Decide(seen.Reading, verb); decision.Action {
	case ActionRefuse:
		return nil, failed("%s", c.refusal(seen))
	case ActionUnavailable:
		return nil, unavailable("%s", c.unservable(seen))
	case ActionReplace:
		return c.replaceHost(ctx, seen)
	case ActionStart:
		dial, err := c.Connect(c.root)
		if err != nil {
			return nil, placeFailure(err)
		}
		if err := startThrough(ctx, dial); err != nil {
			return nil, err
		}
		return dial, nil
	}
	c.warn(seen)
	dial, err := c.Locate(c.root)
	if err != nil {
		return nil, placeFailure(err)
	}
	return dial, nil
}

// replaceHost proves a new host can start, and only then asks the one there to stop.
func (c *call) replaceHost(ctx context.Context, seen sight) (Dial, error) {
	dial, err := c.Prove(c.root)
	if err != nil {
		cause := placeFailure(err)
		if exitCodeOf(cause) == ExitUsage {
			return nil, cause
		}
		return nil, &failure{code: exitCodeOf(cause), msg: "not replacing the host, which stays as it is: " + err.Error()}
	}
	// Only if idle: a run may have started since the host answered, and then it stays and this fails.
	stopped, err := c.stopHost(ctx, true)
	if err != nil {
		return nil, err
	}
	if err := startThrough(ctx, dial); err != nil {
		return nil, err
	}
	if stopped {
		fmt.Fprintf(c.Stderr, "replaced the host: %s\n", c.describeMismatch(seen))
	}
	return dial, nil
}

// warn says what is odd about a host a verb goes on to use.
func (c *call) warn(seen sight) {
	if seen.config != nil {
		fmt.Fprintf(c.Stderr, "warning: %s\n", configProblem(seen.config))
	}
	if seen.Relation != RelationSame || seen.Config == ConfigDiffers {
		fmt.Fprintf(c.Stderr, "note: %s, with %d %s live, so it stays; `%s host restart` replaces it when no run is live\n", c.describeMismatch(seen), seen.LiveRuns, plural(seen.LiveRuns, "run", "runs"), Program)
	}
}

// refusal says why a verb will not go on on a host that serves, and what would let it.
func (c *call) refusal(seen sight) string {
	why := c.describeMismatch(seen)
	switch {
	case seen.LiveRuns > 0:
		return fmt.Sprintf("not starting a run on this host: %s, with %d %s live, and a host is never replaced under live runs. Let them end, or end the host with `%s host stop`, which leaves their agents running until the next host starts, and start again", why, seen.LiveRuns, plural(seen.LiveRuns, "run", "runs"), Program)
	case seen.config != nil:
		return fmt.Sprintf("not starting a run on this host: %s, and it cannot be replaced: %s", why, configProblem(seen.config))
	case seen.Clients > 0:
		return fmt.Sprintf("not starting a run on this host: %s, with %d other %s connected, a desktop app perhaps. Close them, or replace it anyway with `%s host restart`", why, seen.Clients, plural(seen.Clients, "client", "clients"), Program)
	}
	return fmt.Sprintf("not starting a run on this host: %s, and a client does not replace a newer host unasked. Replace it with `%s host restart`", why, Program)
}

// unservable says why no verb can go on here, and what to do about it.
func (c *call) unservable(seen sight) string {
	switch seen.Found {
	case FoundElsewhere:
		return describeElsewhere(c.rootWords(), seen.holder)
	case FoundSilent:
		return fmt.Sprintf("%v. To end it, run `%s host stop --force`", seen.silence, Program)
	case FoundHeld:
		return fmt.Sprintf("this host does not serve the orchestrator: %s. End that process, with `%s --root THAT_ROOT host stop` if it is a host, or give this root another state directory with `%s host configure --state-directory PATH`; then run `%s host restart`", seen.id.GetUnserved().GetDetail(), Program, Program, Program)
	case FoundHolderGone:
		return fmt.Sprintf("this host does not serve the orchestrator: %s. `%s run start` or `%s host restart` replaces it with one that does", seen.id.GetUnserved().GetDetail(), Program, Program)
	}
	return fmt.Sprintf("this host does not serve the orchestrator: %s. Put that right, and `%s run start` or `%s host restart` replaces it with one that does", seen.id.GetUnserved().GetDetail(), Program, Program)
}

// describeElsewhere names the machine a root's host is on, what cannot be done from here and what can.
func describeElsewhere(root string, holder *pb.HostLock) string {
	return fmt.Sprintf("the host of %s is on %s (process %d, release %s), and this machine can neither reach it nor end it. Run this on %s, or give this machine its own root on a local disk with --root or %s", root, holder.GetMachine(), holder.GetPid(), holder.GetRelease(), holder.GetMachine(), gatewayroot.EnvHome)
}

func (c *call) rootWords() string {
	if c.root != "" {
		return c.root
	}
	return "this root"
}

// describeMismatch says why the host is not the one this client would start.
func (c *call) describeMismatch(seen sight) string {
	switch {
	case seen.Found != FoundServing && seen.Found.answers():
		return "the host does not serve the orchestrator: " + seen.id.GetUnserved().GetDetail()
	case seen.Relation == RelationSame && seen.Config == ConfigDiffers:
		return "the host started on another configuration than the one now in its root"
	case seen.Relation == RelationSame:
		return "the host is this client's own"
	case seen.Relation == RelationUnordered && seen.id.GetRelease() == c.Version:
		return fmt.Sprintf("the host is another build of %s than the one this client would start", seen.id.GetRelease())
	}
	return fmt.Sprintf("the host is release %s and this client is release %s", seen.id.GetRelease(), c.Version)
}

// configProblem says what is wrong with the configuration in the root, and how to put it right. The
// error names the file and the key.
func configProblem(err error) string {
	return fmt.Sprintf("the host's configuration does not read, so no new host can start, and a running one keeps what it started with: %v. Correct the file, or forget it with `%s host configure --clear`", err, Program)
}
