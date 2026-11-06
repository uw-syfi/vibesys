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
	// Terminate ends that process and returns once it has exited.
	Terminate(ctx context.Context, root string, holder *pb.HostLock) error
}

const (
	// askBound is how long a host has to answer one call of HostControlService.
	askBound = 5 * time.Second
	// goneEvery and goneTries bound the wait for a host that was told to stop to stop answering.
	goneEvery = 100 * time.Millisecond
	goneTries = 100
)

// hostVerbs are the verbs of `host`.
var hostVerbs = map[string]verb{"status": hostStatus, "stop": hostStop, "restart": hostRestart}

// within is ctx with an end d from now on the environment's clock.
func (c *call) within(ctx context.Context, d time.Duration) (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(ctx)
	timer := c.Clock.After(d)
	go func() {
		select {
		case <-timer:
			cancel()
		case <-ctx.Done():
		}
	}()
	return ctx, cancel
}

// control reaches the host's control service without starting a host. It is ErrNoHost when no host
// serves, and an unavailable failure when one serves and cannot be reached.
func (c *call) control(ctx context.Context) (*client.Control, error) {
	dial, err := c.Locate(c.root)
	if err != nil {
		return nil, unavailable("%v", err)
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
	ctl, err := c.control(ctx)
	if err != nil {
		return nil, err
	}
	defer ctl.Close()
	ctx, cancel := c.within(ctx, askBound)
	defer cancel()
	id, err := ctl.Identify(ctx, c.Version)
	if err != nil {
		return nil, c.unanswered(err)
	}
	return id, nil
}

// unanswered is the failure of a host that was reached and did not answer a call of the control
// service.
func (c *call) unanswered(err error) error {
	if status.Code(err) == codes.Unimplemented {
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
	id, err := c.identify(ctx)
	if errors.Is(err, ErrNoHost) {
		return unavailable("no host is running")
	}
	if err != nil {
		return err
	}
	if *asJSON {
		return printJSON(c.Stdout, id)
	}
	return describeHost(c.Stdout, id, c.Zone)
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
	ctl, err := c.control(ctx)
	if errors.Is(err, ErrNoHost) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	askCtx, cancel := c.within(ctx, askBound)
	reply, err := ctl.Stop(askCtx, onlyIfIdle)
	cancel()
	_ = ctl.Close()
	switch {
	case err == nil && reply.GetRefused() != nil:
		n := reply.GetRefused().GetLiveRuns()
		return false, failed("not stopping the host: %d %s live. Stop them with `%s run stop`, or stop the host without --if-no-live-runs, which leaves their agents running until the next host starts", n, plural(n, "run is", "runs are"), Program)
	case err != nil && status.Code(err) != codes.Unavailable && status.Code(err) != codes.Canceled:
		// The host may close the connection as it stops, before its reply is read; any other failure
		// is a host that does not answer.
		return false, c.unanswered(err)
	}
	return true, c.awaitGone(ctx)
}

// awaitGone waits until no host serves and none holds the host's lock. A host stops answering
// before it has exited, and the next host cannot take over until the lock is free.
func (c *call) awaitGone(ctx context.Context) error {
	for range goneTries {
		dial, err := c.Locate(c.root)
		if err != nil {
			return unavailable("%v", err)
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
	return unavailable("the host was asked to stop and still holds its lock or answers after %s", goneEvery*goneTries)
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
	id, err := c.identify(ctx)
	running := err == nil
	switch {
	case errors.Is(err, ErrNoHost):
	case err != nil:
		return err
	case id.GetLiveRuns() > 0:
		return refuseRestart(id.GetLiveRuns())
	}
	if running {
		// The host is asked to stop only if idle, because a run may have started since it answered.
		if _, err := c.stopHost(ctx, true); err != nil {
			return err
		}
	}
	dial, err := c.Connect(c.root)
	if err != nil {
		return unavailable("%v", err)
	}
	conn, err := dial(ctx)
	if err != nil {
		return unavailable("start the host: %v", err)
	}
	_ = conn.Close()
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
	case pb.UnservedReason_UNSERVED_REASON_STATE_REFUSED:
		return "its state cannot be opened"
	}
	return "unknown reason"
}
