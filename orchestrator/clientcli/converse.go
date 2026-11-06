package clientcli

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli/attachview"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
)

// This file is the shell of `run attach`. It owns the streams, the keys, the clock and the writes to
// the terminal, and decides nothing: every input goes to attachview.Update, and every output it returns is
// carried out here. The terminal is put into key mode when the loop starts and restored on every way
// out of it, including a panic.

const (
	// itemsPage is how many items one read of an agent's history asks for. The service may give fewer.
	itemsPage = 75
	// tickEvery is how often the footer's clock moves.
	tickEvery = time.Second

	eraseLine   = "\r\x1b[2K"
	clearScreen = "\x1b[2J\x1b[H"
)

// converse shows a run until it ends or the caller detaches, starting in the view of start (empty for
// the lead) and replaying history items of each agent viewed (zero for all).
func (c *call) converse(ctx context.Context, s *session, start string, history int) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	controls := c.Terminal && c.Screen != nil
	restore := func() {}
	var width, height int
	if controls {
		r, err := c.Screen.Raw()
		if err != nil {
			fmt.Fprintf(c.Stderr, "note: the terminal would not give keys (%v), so the view is plain lines\n", err)
			controls = false
		} else {
			var once sync.Once
			restore = func() { once.Do(r) }
			width, height = c.Screen.Size()
		}
	}
	defer restore() // a panic, too, leaves the terminal as it was found.
	inputs := make(chan attachview.Input)
	send := func(in attachview.Input) bool {
		select {
		case inputs <- in:
			return true
		case <-ctx.Done():
			return false
		}
	}
	opt := attachview.Options{Controls: controls, Start: start, Only: start != "" && !controls, History: history, Zone: c.Zone, Now: c.Clock.Now(), Width: width, Height: height}
	m, outs := attachview.Begin(opt, s.view, s.history)
	p := &painter{w: c.Stdout, controls: controls, width: width}
	sub := &subscription{c: c, ctx: ctx, client: s.client, send: send}
	defer sub.stop()

	following := false // the run's stream is read from the first replay on, so that its frames never come before it.
	if controls {
		c.startKeyPumps(ctx, send)
	}
	for {
		exit := p.apply(outs, sub)
		if exit == nil && p.err != nil {
			exit = &attachview.Exit{Cause: attachview.ExitDetached}
		}
		if exit != nil {
			p.leave()
			restore()
			return c.ended(s, *exit)
		}
		var in attachview.Input
		select {
		case in = <-inputs:
			if _, broke := in.(attachview.StreamFailed); broke && ctx.Err() != nil {
				in = attachview.Detach{} // a stream that broke because the caller left is the caller leaving.
			}
		case <-ctx.Done():
			in = attachview.Detach{}
		}
		if r, ok := in.(attachview.Resize); ok {
			p.width = r.Width
		}
		m, outs = attachview.Update(m, in)
		if !following && m.Replayed() {
			following = true
			c.startRunPump(ctx, s.feed, send)
		}
	}
}

// ended is what the verb returns for the way the view ended.
func (c *call) ended(s *session, e attachview.Exit) error {
	switch e.Cause {
	case attachview.ExitDetached:
		c.detached(s.view.RunID)
		return nil
	case attachview.ExitBroke:
		return e.Err
	}
	if s.view.Run.GetStatus() == pb.RunStatus_RUN_STATUS_FAILED {
		return &failure{code: ExitFailed}
	}
	return nil
}

// painter writes the outputs. With controls it keeps a footer on the line after the last one printed.
type painter struct {
	w        io.Writer
	controls bool
	footer   string
	// width is the terminal's, or zero when it is not known.
	width int
	err   error
}

func (p *painter) write(text string) {
	if p.err == nil {
		_, p.err = p.w.Write([]byte(text))
	}
}

// apply carries out outputs in order, and returns the attachview.Exit among them, if there is one.
func (p *painter) apply(outs []attachview.Output, sub *subscription) *attachview.Exit {
	for _, o := range outs {
		switch o := o.(type) {
		case attachview.Print:
			p.print(o.Lines)
		case attachview.Clear:
			p.write(clearScreen + p.footer)
		case attachview.SetFooter:
			p.footer = o.Text
			if p.controls {
				p.write(eraseLine + o.Text)
			}
		case attachview.Subscribe:
			sub.open(o)
		case attachview.Sweep:
			sub.sweep(o)
		case attachview.Exit:
			return &o
		}
	}
	return nil
}

// print draws lines. How a line looks is attachview.Render's to say; this adds only the footer.
func (p *painter) print(lines []attachview.Line) {
	if !p.controls {
		for _, line := range lines {
			p.write(attachview.Render(line, p.width) + "\n")
		}
		return
	}
	var b strings.Builder
	if p.footer != "" {
		b.WriteString(eraseLine)
	}
	for _, line := range lines {
		b.WriteString(attachview.Render(line, p.width) + "\n")
	}
	b.WriteString(p.footer)
	p.write(b.String())
}

// leave takes the footer off the screen.
func (p *painter) leave() {
	if p.controls && p.footer != "" {
		p.write(eraseLine)
	}
	p.footer = ""
}

// recovered turns a panic of a pump into the end of the view, so that the loop restores the terminal
// instead of the process dying with it in key mode.
func recovered(send func(attachview.Input) bool, gen uint64) {
	if r := recover(); r != nil {
		send(attachview.StreamFailed{Gen: gen, Err: failed("internal error: %v", r)})
	}
}

// startRunPump reads the run's stream for the life of the view.
func (c *call) startRunPump(ctx context.Context, f *feed, send func(attachview.Input) bool) {
	go func() {
		defer recovered(send, 0)
		for {
			snapshot, change, search, err := f.frame()
			if err != nil {
				send(attachview.StreamFailed{Err: err})
				return
			}
			if !send(attachview.RunFrame{Snapshot: snapshot, Change: change, Search: search}) {
				return
			}
		}
	}()
}

// startKeyPumps reads the keys, the size of the terminal and the footer's clock.
func (c *call) startKeyPumps(ctx context.Context, send func(attachview.Input) bool) {
	go func() {
		defer recovered(send, 0)
		buf := make([]byte, 64)
		for {
			n, err := c.Stdin.Read(buf)
			for _, k := range attachview.ParseKeys(buf[:n]) {
				if !send(k) {
					return
				}
			}
			if err != nil {
				return // no more keys; the view goes on without them.
			}
		}
	}()
	go func() {
		defer recovered(send, 0)
		for {
			select {
			case <-c.Screen.Resizes():
				w, h := c.Screen.Size()
				if !send(attachview.Resize{Width: w, Height: h}) {
					return
				}
			case <-ctx.Done():
				return
			}
		}
	}()
	go func() {
		defer recovered(send, 0)
		for {
			select {
			case <-c.Clock.After(tickEvery):
				if !send(attachview.Tick{Now: c.Clock.Now()}) {
					return
				}
			case <-ctx.Done():
				return
			}
		}
	}()
}

// subscription is the one open stream of an agent. Opening another ends the first.
type subscription struct {
	c      *call
	ctx    context.Context
	client *client.Client
	send   func(attachview.Input) bool
	cancel context.CancelFunc
}

func (s *subscription) stop() {
	if s.cancel != nil {
		s.cancel()
	}
}

func (s *subscription) open(o attachview.Subscribe) {
	s.stop()
	ctx, cancel := context.WithCancel(s.ctx)
	s.cancel = cancel
	go s.c.readAgent(ctx, cancel, s.client, o, s.send)
}

// sweep reads the newest items of an agent once. It is not part of the subscription: a view that
// switched away meanwhile drops the answer by its generation.
func (s *subscription) sweep(o attachview.Sweep) {
	go func() {
		defer recovered(s.send, o.Gen)
		page, err := within(s.ctx, s.c.Clock, serviceBound, "send the agent's newest items", func(ctx context.Context) (*pb.ItemsPage, error) {
			return s.client.ListItems(ctx, o.Agent, math.MaxUint32, itemsPage)
		})
		if err != nil {
			s.send(attachview.StreamFailed{Gen: o.Gen, Err: classify(err)})
			return
		}
		s.send(attachview.AgentSweep{Gen: o.Gen, Final: o.Final, Items: page.GetItems()})
	}()
}

// readAgent reads one agent's conversation: its newest items, older ones back to the limit, then each
// frame as it comes. Every input it sends carries the generation, so that attachview.Update can tell a frame of
// a view that has been left.
func (c *call) readAgent(ctx context.Context, cancel context.CancelFunc, cl *client.Client, o attachview.Subscribe, send func(attachview.Input) bool) {
	defer recovered(send, o.Gen)
	var expired atomic.Bool
	fail := func(err error) {
		switch {
		case expired.Load():
			err = noAnswer("send the agent's items", serviceBound)
		case ctx.Err() != nil:
			return // the view moved on.
		case errors.Is(err, io.EOF):
			err = unavailable("the connection to the orchestrator closed")
		default:
			err = classify(err)
		}
		send(attachview.StreamFailed{Gen: o.Gen, Err: err})
	}
	timer := c.Clock.After(serviceBound)
	first := make(chan struct{})
	go func() {
		select {
		case <-timer:
			expired.Store(true)
			cancel()
		case <-first:
		case <-ctx.Done():
		}
	}()
	size := uint32(itemsPage)
	if o.Limit > 0 {
		size = uint32(min(o.Limit, itemsPage))
	}
	stream, err := cl.WatchAgent(ctx, o.Agent, size, 0)
	var frame *pb.WatchAgentResponse
	if err == nil {
		frame, err = stream.Recv()
	}
	close(first)
	if err != nil {
		fail(err)
		return
	}
	page := frame.GetPage()
	if page == nil {
		fail(failed("the orchestrator began the stream of an agent with something other than a page"))
		return
	}
	items := sortedItems(page.GetItems())
	before := page.GetNextBeforeOrdinal()
	for before != 0 && (o.Limit == 0 || len(items) < o.Limit) {
		older, err := within(ctx, c.Clock, serviceBound, "send older items", func(ctx context.Context) (*pb.ItemsPage, error) {
			return cl.ListItems(ctx, o.Agent, before, itemsPage)
		})
		if err != nil {
			fail(err)
			return
		}
		if len(older.GetItems()) == 0 || older.GetNextBeforeOrdinal() >= before {
			break
		}
		items = append(sortedItems(older.GetItems()), items...)
		before = older.GetNextBeforeOrdinal()
	}
	if o.Limit > 0 && len(items) > o.Limit {
		items = items[len(items)-o.Limit:]
	}
	if !send(attachview.AgentHistory{Gen: o.Gen, Items: items}) {
		return
	}
	for {
		frame, err := stream.Recv()
		if err != nil {
			fail(err)
			return
		}
		var in attachview.Input
		switch body := frame.GetBody().(type) {
		case *pb.WatchAgentResponse_Item:
			in = attachview.AgentFrame{Gen: o.Gen, Item: body.Item}
		case *pb.WatchAgentResponse_Turn:
			in = attachview.AgentFrame{Gen: o.Gen, Turn: body.Turn}
		default:
			continue // progress on a running item is not shown: the finished item is.
		}
		if !send(in) {
			return
		}
	}
}

func sortedItems(items []*pb.Item) []*pb.Item {
	out := slices.Clone(items)
	slices.SortStableFunc(out, func(a, b *pb.Item) int { return int(a.GetOrdinal()) - int(b.GetOrdinal()) })
	return out
}
