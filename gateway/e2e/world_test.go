//go:build e2e

package e2e

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
)

// world is the run as the stream of runs shows it: the last state of each agent and each message, folded
// from the events in order. It is a reading of the daemon's own output, so what a test asserts on
// it is what an operator's client would see.
type world struct {
	agents   map[string]*agentView
	messages map[string]*messageView
	run      runView
	// last is the sequence of the last event folded in, where the next watch resumes.
	last uint64
}

type agentView struct {
	ID      string `json:"id"`
	Parent  string `json:"parentAgentId"`
	Role    string `json:"role"`
	Harness string `json:"harness"`
	Status  string `json:"status"`
	Plan    int    `json:"planNumber"`
}

type messageView struct {
	ID        string `json:"id"`
	From      string `json:"fromAgentId"`
	To        string `json:"toAgentId"`
	Delivery  string `json:"delivery"`
	LeadBrief *struct {
		Task        string `json:"task"`
		FanoutLimit int    `json:"fanoutLimit"`
		Steering    string `json:"steering"`
	} `json:"leadBrief"`
	Handoff *struct {
		PlanNumber int    `json:"planNumber"`
		Claim      string `json:"claim"`
		Commit     string `json:"commit"`
	} `json:"handoff"`
	Report *struct {
		Headline string `json:"headline"`
	} `json:"report"`
}

type runView struct {
	ID          string `json:"id"`
	Status      string `json:"status"`
	FanoutLimit int    `json:"fanoutLimit"`
	Lead        string `json:"leadAgentId"`
}

func newWorld() *world {
	return &world{agents: map[string]*agentView{}, messages: map[string]*messageView{}}
}

// fold applies one line of the stream of runs. A line that is not an event (a snapshot) changes nothing.
func (w *world) fold(line string) {
	var l struct {
		Event *struct {
			Sequence       string       `json:"sequence"`
			AgentUpdated   *agentView   `json:"agentUpdated"`
			MessageUpdated *messageView `json:"messageUpdated"`
			RunUpdated     *runView     `json:"runUpdated"`
		} `json:"event"`
	}
	if json.Unmarshal([]byte(line), &l) != nil || l.Event == nil {
		return
	}
	if n, err := strconv.ParseUint(l.Event.Sequence, 10, 64); err == nil {
		w.last = n
	}
	if a := l.Event.AgentUpdated; a != nil {
		w.agents[a.ID] = a
	}
	if m := l.Event.MessageUpdated; m != nil {
		w.messages[m.ID] = m
	}
	if run := l.Event.RunUpdated; run != nil {
		w.run = *run
	}
}

// agentsWhere lists the agents satisfying keep, in no particular order.
func (w *world) agentsWhere(keep func(*agentView) bool) []*agentView {
	var out []*agentView
	for _, a := range w.agents {
		if keep(a) {
			out = append(out, a)
		}
	}
	return out
}

// report is the headline of the lead's report, or "" if it has sent none.
func (w *world) report() string {
	for _, m := range w.messages {
		if m.Report != nil {
			return m.Report.Headline
		}
	}
	return ""
}

// observe resumes the watch after the world's last event and folds what it prints until done
// holds, failing after the bound or when the run fails.
func (r *rig) observe(what string, w *world, done func() bool) {
	r.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), bound)
	defer cancel()
	next := r.watch(ctx, w.last)
	var seen []string
	for {
		line, err := next()
		if err != nil {
			break
		}
		seen = append(seen, line)
		w.fold(line)
		if done() {
			return
		}
		if w.run.Status == "RUN_STATUS_FAILED" {
			break
		}
	}
	r.t.Fatalf("watch ended without %s within %s (run %s); it printed:\n%s\nserve log:\n%s", what, bound, w.run.Status, strings.Join(seen, "\n"), r.serveLog())
}
