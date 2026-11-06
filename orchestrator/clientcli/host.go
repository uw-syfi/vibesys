package clientcli

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostlock"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
)

// ErrNoHost is what a Dial from Env.Locate returns when no host serves, as against one that serves
// and does not answer.
var ErrNoHost = errors.New("no host is running")

// HostProcesses is what `host stop --force` needs of the machine when a host holds its lock and
// does not answer.
type HostProcesses interface {
	// Holder is the record of the process that holds the host lock of a --root value ("" for the
	// default), or nil when no process does. A lock that is free names nobody, whatever it once held.
	Holder(root string) (*pb.HostLock, error)
	// Terminate ends that process and returns once it has exited. It refuses a process on another
	// machine, whose number means nothing here.
	Terminate(ctx context.Context, root string, holder *pb.HostLock) error
	// Machine is the name of this machine, as a lock's record names one.
	Machine() string
}

const (
	// askBound is how long a host has to answer one call of HostControlService.
	askBound = 5 * time.Second
	// goneEvery and goneTries bound the wait for a host that was told to stop to stop answering.
	goneEvery = 100 * time.Millisecond
	goneTries = 100
	// clientLookEvery and clientLooks bound how long a client that may be gone keeps a host from being
	// replaced.
	clientLookEvery = 100 * time.Millisecond
	clientLooks     = 5
)

// hostVerbs are the verbs of `host`.
var hostVerbs = map[string]verb{"status": hostStatus, "stop": hostStop, "restart": hostRestart, "configure": hostConfigure}

// control reaches the host's control service without starting a host. It is ErrNoHost when no host
// serves, and an unavailable failure when one serves and cannot be reached.
func (c *call) control(ctx context.Context) (*client.Control, error) {
	dial, err := c.Locate(c.root)
	if err != nil {
		return nil, placeFailure(err)
	}
	conn, err := dial(ctx)
	if errors.Is(err, ErrNoHost) {
		return nil, ErrNoHost
	}
	if err != nil {
		return nil, unavailable("a host is running and cannot be reached: %v", err)
	}
	return client.DialControl(conn)
}

// identify asks the host what it is.
func (c *call) identify(ctx context.Context) (*pb.IdentifyResponse, error) {
	asked, expired, cancel := bound(ctx, c.Clock, askBound)
	defer cancel()
	ctl, err := c.control(asked)
	if err != nil {
		return nil, c.unanswered(ctx, expired, err)
	}
	defer ctl.Close()
	id, err := ctl.Identify(asked, c.Version)
	if err != nil {
		return nil, c.unanswered(ctx, expired, err)
	}
	return id, nil
}

// unanswered is the failure of a host that was reached and did not answer a call of the control
// service: one that ran out of its time, one that is too old to know the call, or a caller that left.
// ErrNoHost and failures that are already what they should be pass through.
func (c *call) unanswered(ctx context.Context, expired func() bool, err error) error {
	var f *failure
	switch {
	case errors.Is(err, ErrNoHost):
		return err
	case expired():
		return unavailable("a host is running and did not answer within %s", askBound)
	case ctx.Err() != nil:
		return failed("interrupted")
	case errors.As(err, &f):
		return err
	case status.Code(err) == codes.Unimplemented:
		return unavailable("a host is running that predates host control, so it cannot say what it is or be asked to stop; if it holds no run you need, end it by hand, or install again with --force and leave it be")
	}
	return unavailable("a host is running and does not answer: %v", err)
}

func hostStatus(ctx context.Context, c *call, args []string) error {
	fs := flags("host status")
	asJSON := fs.Bool("json", false, "print the host's identity as JSON")
	positional, err := parse(fs, args)
	if err != nil {
		return err
	}
	if len(positional) != 0 {
		return usageError("host status takes no argument")
	}
	seen, err := c.look(ctx)
	switch {
	case err != nil:
		return err
	case seen.Found == FoundSilent:
		return seen.silence
	case seen.Found == FoundElsewhere:
		return unavailable("%s", c.unservable(seen))
	case seen.Found == FoundNothing:
		if _, err := c.Configs.Load(c.root); err != nil {
			return unavailable("no host is running, and none can start until this is put right: %s", configProblem(err))
		}
		return unavailable("no host is running")
	}
	if seen.config != nil {
		// On stderr, so that --json stays what it is: the host's identity.
		fmt.Fprintf(c.Stderr, "warning: %s\n", configProblem(seen.config))
	}
	if *asJSON {
		return printJSON(c.Stdout, seen.id)
	}
	if err := describeHost(c.Stdout, seen.id, c.Zone); err != nil {
		return err
	}
	if seen.Found != FoundServing {
		_, err = fmt.Fprintln(c.Stdout, "to do: "+c.unservable(seen))
	}
	return err
}

func hostStop(ctx context.Context, c *call, args []string) error {
	fs := flags("host stop")
	idle := fs.Bool("if-no-live-runs", false, "stop only when no run is live")
	force := fs.Bool("force", false, "end a host that does not answer, by the process its lock names")
	positional, err := parse(fs, args)
	if err != nil {
		return err
	}
	if len(positional) != 0 {
		return usageError("host stop takes no argument")
	}
	seen, err := c.look(ctx)
	if err != nil {
		return err
	}
	switch Decide(seen.Reading, VerbStop).Action {
	case ActionDone:
		_, err = fmt.Fprintln(c.Stdout, "no host is running")
		return err
	case ActionUnavailable:
		if *force && seen.Found == FoundSilent {
			return c.forceStop(ctx, seen.silence)
		}
		return unavailable("%s", c.unservable(seen))
	}
	stopped, err := c.stopHost(ctx, *idle)
	switch {
	case err == nil && stopped:
		_, err = fmt.Fprintln(c.Stdout, "the host stopped")
		return err
	case err == nil:
		_, err = fmt.Fprintln(c.Stdout, "no host is running")
		return err
	case !*force || exitCodeOf(err) != ExitUnavailable:
		return err
	}
	return c.forceStop(ctx, err)
}

// stopHost asks the host to stop and waits until it has. It is false, with no error, when no host
// was running, and a failure when the host refuses (it has live runs and was asked to stop only if
// it has none) or does not answer.
func (c *call) stopHost(ctx context.Context, onlyIfIdle bool) (bool, error) {
	asked, expired, cancel := bound(ctx, c.Clock, askBound)
	defer cancel()
	ctl, err := c.control(asked)
	if errors.Is(err, ErrNoHost) {
		return false, nil
	}
	if err != nil {
		return false, stopFailure(c.unanswered(ctx, expired, err))
	}
	reply, err := ctl.Stop(asked, onlyIfIdle)
	_ = ctl.Close()
	switch {
	case err == nil && reply.GetRefused() != nil:
		n := reply.GetRefused().GetLiveRuns()
		return false, failed("not stopping the host: %d %s live. Stop them with `%s run stop`, or stop the host without --if-no-live-runs, which leaves their agents running until the next host starts", n, plural(n, "run is", "runs are"), Program)
	case err != nil && (expired() || ctx.Err() != nil || status.Code(err) != codes.Unavailable):
		// The host closing the connection as it stops, before its reply is read, is Unavailable. The
		// end of this call's own time reads as Canceled, and is a host that does not answer.
		return false, stopFailure(c.unanswered(ctx, expired, err))
	}
	return true, c.awaitGone(ctx)
}

// stopFailure is a host that could not be asked to stop, with the way out.
func stopFailure(err error) error {
	if exitCodeOf(err) != ExitUnavailable {
		return err
	}
	return unavailable("%v. To end it, run `%s host stop --force`", err, Program)
}

// awaitGone waits until no host serves and none holds the host's lock. A host stops answering
// before it has exited, and the next host cannot take over until the lock is free.
func (c *call) awaitGone(ctx context.Context) error {
	for range goneTries {
		dial, err := c.Locate(c.root)
		if err != nil {
			return placeFailure(err)
		}
		conn, err := dial(ctx)
		if errors.Is(err, ErrNoHost) && c.lockFree() {
			return nil
		}
		if err == nil {
			_ = conn.Close()
		}
		select {
		case <-ctx.Done():
			return failed("interrupted")
		case <-c.Clock.After(goneEvery):
		}
	}
	return unavailable("the host was asked to stop and still holds its lock or answers after %s. To end it, run `%s host stop --force`", goneEvery*goneTries, Program)
}

// lockFree is whether no process holds the host's lock; true when this client cannot look.
func (c *call) lockFree() bool {
	if c.Hosts == nil {
		return true
	}
	holder, err := c.Hosts.Holder(c.root)
	return err == nil && holder == nil
}

// forceStop ends the process that holds the host's lock, for a host that does not answer.
func (c *call) forceStop(ctx context.Context, unanswered error) error {
	holder, err := c.Hosts.Holder(c.root)
	if err != nil {
		return failed("%v; %v", unanswered, err)
	}
	if holder == nil {
		_, err = fmt.Fprintln(c.Stdout, "no host holds the lock, so none is running")
		return err
	}
	if hostlock.Elsewhere(holder, c.Hosts.Machine()) {
		return unavailable("%s", describeElsewhere(c.rootWords(), holder))
	}
	if err := c.Hosts.Terminate(ctx, c.root, holder); err != nil {
		return failed("end process %d of release %s: %v", holder.GetPid(), holder.GetRelease(), err)
	}
	_, err = fmt.Fprintf(c.Stdout, "ended the host (process %d, release %s)\n", holder.GetPid(), holder.GetRelease())
	return err
}

func hostRestart(ctx context.Context, c *call, args []string) error {
	fs := flags("host restart")
	positional, err := parse(fs, args)
	if err != nil {
		return err
	}
	if len(positional) != 0 {
		return usageError("host restart takes no argument")
	}
	seen, err := c.look(ctx)
	if err != nil {
		return err
	}
	var dial Dial
	switch Decide(seen.Reading, VerbRestart).Action {
	case ActionRefuse:
		return refuseRestart(seen.LiveRuns)
	case ActionUnavailable:
		return unavailable("%s", c.unservable(seen))
	case ActionStart:
		dial, err = c.Connect(c.root)
	default:
		if seen.Clients > 0 {
			fmt.Fprintf(c.Stderr, "note: %d other %s connected to this host will lose their connection\n", seen.Clients, plural(seen.Clients, "client is", "clients are"))
		}
		if dial, err = c.Prove(c.root); err != nil {
			break
		}
		// The host is asked to stop only if idle, because a run may have started since it answered.
		if _, err := c.stopHost(ctx, true); err != nil {
			return err
		}
	}
	if err != nil {
		cause := placeFailure(err)
		if seen.Found != FoundNothing && exitCodeOf(cause) != ExitUsage {
			return &failure{code: exitCodeOf(cause), msg: "not restarting the host, which stays as it is: " + err.Error()}
		}
		return cause
	}
	if err := startThrough(ctx, dial); err != nil {
		return err
	}
	now, err := c.identify(ctx)
	if err != nil {
		return err
	}
	return describeHost(c.Stdout, now, c.Zone)
}

func refuseRestart(live uint32) error {
	return failed("not restarting the host: %d %s live. A host can be replaced under live runs only once a new host can take their sessions over, and cannot yet. Stop them with `%s run stop` first", live, plural(live, "run is", "runs are"), Program)
}

func plural(n uint32, one, many string) string {
	if n == 1 {
		return one
	}
	return many
}

// describeHost prints what a host said it is, in words.
func describeHost(w io.Writer, id *pb.IdentifyResponse, zone *time.Location) error {
	var b strings.Builder
	fmt.Fprintf(&b, "host: release %s, %s\n", id.GetRelease(), relation(id.GetRelation()))
	if zone == nil {
		zone = time.UTC
	}
	fmt.Fprintf(&b, "started: %s\n", id.GetStartedAt().AsTime().In(zone).Format("2006-01-02 15:04:05 MST"))
	fmt.Fprintf(&b, "live runs: %d\n", id.GetLiveRuns())
	fmt.Fprintf(&b, "other clients connected: %d\n", id.GetConnectedClients())
	if unserved := id.GetUnserved(); unserved != nil {
		fmt.Fprintf(&b, "orchestrator: not served here (%s)", unservedWords(unserved))
		if unserved.GetDetail() != "" {
			fmt.Fprintf(&b, ": %s", unserved.GetDetail())
		}
		b.WriteString("\n")
	} else {
		b.WriteString("orchestrator: serving\n")
	}
	for _, p := range id.GetParts() {
		if problem := p.GetProblem(); problem != nil {
			fmt.Fprintf(&b, "%s: cannot be used: %s\n", partName(p.GetPart()), problem.GetDetail())
		} else {
			fmt.Fprintf(&b, "%s: ready, from %s\n", partName(p.GetPart()), originWords(p.GetReady().GetOrigin()))
		}
	}
	_, err := io.WriteString(w, b.String())
	return err
}

func relation(r pb.ReleaseRelation) string {
	switch r {
	case pb.ReleaseRelation_RELEASE_RELATION_SAME:
		return "the same as this client"
	case pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER:
		return "older than this client"
	case pb.ReleaseRelation_RELEASE_RELATION_HOST_NEWER:
		return "newer than this client"
	}
	return "not comparable with this client"
}

func partName(p pb.HostPart) string {
	switch p {
	case pb.HostPart_HOST_PART_SESSION_HOLDER:
		return "session holder"
	case pb.HostPart_HOST_PART_SKILLS:
		return "skills"
	case pb.HostPart_HOST_PART_JOB_TOOL:
		return "job tool"
	}
	return p.String()
}

func originWords(o pb.PartOrigin) string {
	if o == pb.PartOrigin_PART_ORIGIN_OPERATOR {
		return "the operator's choice"
	}
	return "the release"
}

func unservedWords(u *pb.Unserved) string {
	switch u.GetReason() {
	case pb.UnservedReason_UNSERVED_REASON_HELD_ELSEWHERE:
		if u.GetHeldByRelease() != "" {
			return "held by another host, release " + u.GetHeldByRelease()
		}
		return "held by another host"
	case pb.UnservedReason_UNSERVED_REASON_HOLDER_GONE:
		return "another host held its state, and has gone"
	case pb.UnservedReason_UNSERVED_REASON_STATE_REFUSED:
		return "its state cannot be opened"
	}
	return "unknown reason"
}
