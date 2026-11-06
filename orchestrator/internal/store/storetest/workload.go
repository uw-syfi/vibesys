package storetest

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"math/rand/v2"
	"slices"
	"strings"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
)

// Workload is a seeded stream of commit results taken from the real decision core: runs start,
// sessions open, turns stream items and end, agents are spawned and stopped, mail is sent, and
// timers are armed and fire. Nothing in it is invented for the store, so the events have the shape
// a daemon commits and the effects are the ones the core asks for.
//
// One seed gives one stream, which is how a crash test in a child process and the parent that
// checks it agree on what was committed.
type Workload struct {
	r       *rand.Rand
	state   core.State
	now     time.Time
	nonce   int
	counter int
	offsets map[core.Session]uint64
	pending []core.Effect
	turns   []turnPlan
	timers  []core.ArmTimer
	// BigOutputs is the chance, in percent, that a tool output is large. Zero means 3.
	BigOutputs int
}

type turnPlan struct {
	session core.Session
	turn    core.TurnID
	left    int
	// running is the id of the item still being written, if any.
	running core.ItemID
	size    int
}

// NewWorkload starts a stream from a seed.
func NewWorkload(seed uint64) *Workload {
	return &Workload{
		r: rand.New(rand.NewPCG(seed, seed^0x9e3779b97f4a7c15)), now: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC),
		offsets: map[core.Session]uint64{},
	}
}

// State is the state after everything Next has returned.
func (w *Workload) State() core.State { return w.state }

// Now is the time of the last input.
func (w *Workload) Now() time.Time { return w.now }

// Next decides inputs until one changes something, and returns what it produced.
func (w *Workload) Next() store.Result {
	for {
		w.now = w.now.Add(time.Duration(1+w.r.IntN(5)) * time.Second)
		w.nonce++
		body := w.choose()
		if body == nil {
			continue
		}
		next, events, effects := core.Decide(w.state, core.Input{At: w.now, Nonce: core.Nonce(fmt.Sprintf("n%d", w.nonce)), Body: body})
		w.state = next
		w.pending = append(w.pending, effects...)
		if len(events) > 0 {
			return store.Result{Events: events, Effects: effects}
		}
	}
}

func (w *Workload) id(prefix string) string {
	w.counter++
	return fmt.Sprintf("%s-%d", prefix, w.counter)
}

func (w *Workload) command(caller core.Caller, body core.CommandBody) core.Command {
	id := w.id("cmd")
	return core.Command{ID: core.CommandID(id), Digest: digest(id), Caller: caller, Body: body}
}

// digest stands for the digest of a command's content: any 64 hex digits the contract accepts.
func digest(id string) core.Digest {
	sum := sha256.Sum256([]byte(id))
	return core.Digest(hex.EncodeToString(sum[:]))
}

func (w *Workload) fact(s core.Session, body core.FactBody) core.SessionFact {
	w.offsets[s]++
	return core.SessionFact{Session: s, Offset: w.offsets[s], Body: body}
}

// choose picks the next input, or nothing when it only moved its own bookkeeping.
func (w *Workload) choose() core.InputBody {
	if len(w.pending) > 0 && w.r.IntN(10) < 6 {
		e := w.pending[0]
		w.pending = w.pending[1:]
		return w.answer(e)
	}
	switch n := w.r.IntN(100); {
	case n < 55 && len(w.turns) > 0:
		return w.advance()
	case n < 62 && len(w.timers) > 0:
		i := w.r.IntN(len(w.timers))
		t := w.timers[i]
		w.timers = slices.Delete(w.timers, i, i+1)
		return core.TimerFired{Timer: t}
	default:
		return w.newCommand()
	}
}

// answer plays the part of whatever carries out an effect.
func (w *Workload) answer(e core.Effect) core.InputBody {
	switch e := e.(type) {
	case core.LaunchSession:
		return w.fact(e.Session, core.SessionOpened{
			ResumeToken: "conv-" + string(e.Session.Agent), Capabilities: core.Capabilities{Steers: true, Resumes: true},
		})
	case core.StartTurn:
		w.turns = append(w.turns, turnPlan{session: e.Session, turn: e.Turn, left: 1 + w.r.IntN(6)})
	case core.Steer:
		return w.fact(e.Session, core.SteerOutcome{Message: e.Message.ID, Accepted: w.r.IntN(5) > 0})
	case core.Interrupt:
		return w.fact(e.Session, core.TurnEnded{Turn: e.Turn, Status: core.TurnInterrupted})
	case core.CloseSession:
		return w.fact(e.Session, core.SessionClosed{})
	case core.ArmTimer:
		w.timers = append(w.timers, e)
	}
	return nil
}

// advance streams one more item of a running turn, or ends it.
func (w *Workload) advance() core.InputBody {
	i := w.r.IntN(len(w.turns))
	p := &w.turns[i]
	if p.left == 0 && p.running == "" {
		cost := float64(w.r.IntN(500)) / 100
		in, out := uint64(w.r.IntN(50000)), uint64(w.r.IntN(5000))
		s, t := p.session, p.turn
		w.turns = slices.Delete(w.turns, i, i+1)
		return w.fact(s, core.TurnEnded{Turn: t, Status: core.TurnCompleted, Usage: core.Usage{InputTokens: &in, OutputTokens: &out, CostUSD: &cost}})
	}
	if p.running != "" {
		// The running item finishes, with its whole output.
		id := p.running
		p.running = ""
		return w.fact(p.session, core.ItemObserved{ID: id, Turn: p.turn, Status: core.ItemFinished, Body: w.tool(p.size * 3)})
	}
	p.left--
	p.size = w.size()
	id := core.ItemID(w.id("item"))
	if w.r.IntN(2) == 0 {
		return w.fact(p.session, core.ItemObserved{ID: id, Turn: p.turn, Status: core.ItemFinished, Body: core.Assistant{Text: w.text(p.size)}})
	}
	p.running = id
	return w.fact(p.session, core.ItemObserved{ID: id, Turn: p.turn, Status: core.ItemRunning, Body: w.tool(p.size)})
}

func (w *Workload) size() int {
	big := w.BigOutputs
	if big == 0 {
		big = 3
	}
	if w.r.IntN(100) < big {
		return 50_000 + w.r.IntN(250_000)
	}
	return 50 + w.r.IntN(1500)
}

func (w *Workload) text(n int) string { return strings.Repeat("x", max(n, 1)) }

func (w *Workload) tool(n int) core.ToolCall {
	code := int32(w.r.IntN(2))
	return core.ToolCall{Tool: "bash", ProviderCallID: w.id("call"), ArgsJSON: `{"command":"ls"}`, Output: w.text(n), ExitCode: &code}
}

// newCommand asks for something of the core, as a client or an agent would.
func (w *Workload) newCommand() core.InputBody {
	runs := w.state.Runs()
	var live []core.Run
	for _, r := range runs {
		if r.Status.Live() {
			live = append(live, r)
		}
	}
	if len(live) == 0 || (len(runs) < 6 && w.r.IntN(25) == 0) {
		var maxTime time.Duration
		if w.r.IntN(3) == 0 {
			maxTime = time.Duration(1+w.r.IntN(48)) * time.Hour
		}
		return w.command(core.Operator{}, core.StartRun{
			Repository: "repo-a", Task: "task-" + w.id("t"), FanoutLimit: uint32(1 + w.r.IntN(4)), MaxTime: maxTime,
			Lead: core.AgentSpec{Harness: core.ClaudeCode, Model: "model-a", Effort: "high"}, Brief: "do the task",
		})
	}
	run := live[w.r.IntN(len(live))]
	agents := w.state.Agents(run.ID)
	agent := agents[w.r.IntN(len(agents))]
	switch n := w.r.IntN(100); {
	case n < 30:
		return w.command(core.AgentCaller{Agent: run.Lead}, core.SpawnAgent{
			Run: run.ID, Role: core.RoleWorker, PlanNumber: uint32(w.r.IntN(5)), Brief: "implement plan",
		})
	case n < 55:
		return w.command(core.Operator{}, core.SendMessage{Agent: agent.ID, Text: "steer " + w.id("m")})
	case n < 70 && agent.Role != core.RoleLead:
		return w.command(core.AgentCaller{Agent: agent.ID}, core.PostHandoff{Handoff: core.Handoff{
			Claim: "faster", Body: core.WorkerCandidate{Commit: core.Commit("abc"), ParentCommit: core.Commit("def"), Branch: "b", Checks: "ok", Measurement: "1"},
		}})
	case n < 80 && agent.Role != core.RoleLead:
		return w.command(core.AgentCaller{Agent: run.Lead}, core.StopAgent{Agent: agent.ID})
	case n < 84:
		return w.command(core.Operator{}, core.StopRun{Run: run.ID})
	case n < 90:
		return w.command(core.AgentCaller{Agent: run.Lead}, core.PostReport{Report: core.Report{Kind: core.ReportProgress, Headline: "status"}})
	default:
		// A command the core refuses: a receipt is still committed, and with no run.
		return w.command(core.Operator{}, core.StopRun{Run: "run-none"})
	}
}
