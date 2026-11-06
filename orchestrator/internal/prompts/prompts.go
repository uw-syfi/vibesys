// Package prompts words what an agent is sent for a message.
//
// A message the orchestrator addresses to an agent is typed data. The host turns it into the text
// the agent reads when it sends the command that carries it, and the process holding the agent is
// handed that text and nothing else: it never learns what a message was, which skill it names or
// which environment variable the words mention. Each message body has one template under
// templates/; Render passes data and nothing else, so no prompt text is built in code.
//
// Render is pure: the same message gives the same bytes on every call, which is what lets a
// delivery that is retried send the words the first attempt sent.
package prompts

import (
	"embed"
	"errors"
	"fmt"
	"strings"
	"text/template"
	"time"
	"unicode"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

//go:embed templates/*.md.tmpl
var files embed.FS

// templates holds one template per message body, named by its file.
var templates = template.Must(template.New("prompts").Option("missingkey=error").ParseFS(files, "templates/*.md.tmpl"))

// ErrNoPrompt is returned for a message that has no body, so there is nothing to word.
var ErrNoPrompt = errors.New("the message has no prompt")

// Render returns the words an agent is sent for m. It fails with ErrNoPrompt for a message with no
// body. A text or a brief of nothing but blanks renders to nothing, as it always has: the effect
// must still be carried out, because a turn that is never started would hold back its session.
func Render(m core.Message) (string, error) {
	name, data, err := dataOf(m)
	if err != nil {
		return "", err
	}
	var out strings.Builder
	if err := templates.ExecuteTemplate(&out, name+".md.tmpl", data); err != nil {
		return "", fmt.Errorf("render the %s prompt of message %s: %w", name, m.ID, err)
	}
	return strip(out.String()), nil
}

// strip removes the whitespace around text. It is the whitespace Python's str.strip removes, which
// the prompts were first rendered with, so the words stay byte for byte what agents have been sent:
// it is unicode.IsSpace and the four ASCII separators (0x1c to 0x1f) that Python counts and Go does
// not.
func strip(text string) string {
	return strings.TrimFunc(text, func(r rune) bool {
		return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f)
	})
}

// The data each template is given. A field is a thing the words say, spelled as a word.
type (
	textData struct{ Text string }

	leadBriefData struct {
		Task        string
		FanoutLimit uint32
		Steering    string
	}

	// handoffData holds the fields of every kind of handoff; Kind says which the template reads.
	handoffData struct {
		From                                                 core.AgentID
		PlanNumber                                           uint32
		Kind                                                 string
		Claim, Learned, Next, OpenItems                      string
		Commit, ParentCommit, Revision                       core.Commit
		Branch, Checks, Measurement, Blocker                 string
		Plan, Confidence, Unestablished, DecidingMeasurement string
		Verdict                                              core.Verdict
		Reasons, Applied, Unapplied                          string
		Question, Answer, Evidence, Unsupported, Informs     string
	}

	reportData struct {
		From        core.AgentID
		Headline    string
		Changed     string
		InFlight    string
		Decisions   string
		OpenForUser string
	}

	jobFinishedData struct {
		Job     core.JobID
		Outcome core.JobState
		Detail  string
	}

	subagentSettledData struct {
		Agent  core.AgentID
		Status core.AgentStatus
		Detail string
	}

	// runLimitReachedData says which limit the run reached and what is left to say about it. HasSpent
	// is false when no turn has reported a cost.
	runLimitReachedData struct {
		Limit          core.RunLimit
		ElapsedMinutes uint32
		HasSpent       bool
		Spent          float64
		MaxCost        float64
		GraceMinutes   uint32
	}

	leadIdleReminderData struct {
		IdleMinutes uint32
		Subagents   uint32
		Workers     uint32
		Judges      uint32
	}
)

// dataOf names the template of m's body and gathers the data it renders.
func dataOf(m core.Message) (string, any, error) {
	switch b := m.Body.(type) {
	case core.Text:
		return "text", textData{Text: b.Text}, nil
	case core.Brief:
		return "brief", textData{Text: b.Text}, nil
	case core.LeadBrief:
		return "lead_brief", leadBriefData(b), nil
	case core.Handoff:
		return "handoff", handoffDataOf(m.From, b), nil
	case core.Report:
		return "report", reportData{
			From: m.From, Headline: b.Headline, Changed: b.Changed, InFlight: b.InFlight,
			Decisions: b.Decisions, OpenForUser: b.OpenForUser,
		}, nil
	case core.JobFinished:
		return "job_finished", jobFinishedData{Job: b.Job, Outcome: b.Outcome, Detail: b.Detail}, nil
	case core.SubagentSettled:
		return "subagent_settled", subagentSettledData{Agent: b.Agent, Status: b.Status, Detail: b.Detail}, nil
	case core.HandoffReminder:
		return "handoff_reminder", struct{}{}, nil
	case core.RunLimitReached:
		d := runLimitReachedData{
			Limit: b.Limit, ElapsedMinutes: uint32(b.Elapsed / time.Minute), MaxCost: b.MaxCost, GraceMinutes: uint32(b.Grace / time.Minute),
		}
		if b.Spent != nil {
			d.HasSpent, d.Spent = true, *b.Spent
		}
		return "run_limit_reached", d, nil
	case core.LeadIdleReminder:
		// Whole seconds in a uint32, as the message is stored, so the minutes do not depend on
		// whether the message was read back or is still the one that was built.
		seconds := uint32(b.Idle / time.Second)
		return "lead_idle_reminder", leadIdleReminderData{
			IdleMinutes: seconds / 60, Subagents: b.Subagents, Workers: b.Workers, Judges: b.Judges,
		}, nil
	}
	return "", nil, fmt.Errorf("message %s has no body: %w", m.ID, ErrNoPrompt)
}

func handoffDataOf(from core.AgentID, h core.Handoff) handoffData {
	d := handoffData{From: from, PlanNumber: h.PlanNumber, Claim: h.Claim, Learned: h.Learned, Next: h.Next, OpenItems: h.OpenItems}
	switch b := h.Body.(type) {
	case core.WorkerCandidate:
		d.Kind, d.Commit, d.ParentCommit, d.Branch, d.Checks, d.Measurement = "candidate", b.Commit, b.ParentCommit, b.Branch, b.Checks, b.Measurement
	case core.WorkerNoCandidate:
		d.Kind, d.Blocker = "no_candidate", b.Blocker
	case core.PlannerPlan:
		d.Kind, d.Plan, d.Confidence, d.Unestablished, d.DecidingMeasurement = "plan", b.Plan, b.Confidence, b.Unestablished, b.DecidingMeasurement
	case core.JudgeFinding:
		d.Kind, d.Commit, d.Verdict, d.Reasons = "verdict", b.Commit, b.Verdict, b.Reasons
	case core.CombinerResult:
		d.Kind, d.Commit, d.Branch, d.Applied, d.Unapplied, d.Checks, d.Measurement = "combined", b.Commit, b.Branch, b.Applied, b.Unapplied, b.Checks, b.Measurement
	case core.ProfilerFindings:
		d.Kind, d.Question, d.Revision, d.Answer, d.Evidence, d.Unsupported, d.Informs = "findings", b.Question, b.Revision, b.Answer, b.Evidence, b.Unsupported, b.Informs
	}
	return d
}
