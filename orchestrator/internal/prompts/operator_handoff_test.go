package prompts_test

import (
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/descriptorpb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/prompts"
)

// VibeSys is fully autonomous: the lead decides every open question itself, records it under
// decisions and goes on. Only a hard external block (no disk space, no quota, a host that is gone)
// stops a run, and it shows as the run's ended or blocked state with its cause, never as a question.
// These tests hold that rule in two parts, and neither is a word list over all prose.
//
// The structural part: the fields of Report are a closed list, so a new field fails here until
// someone reads it against the rule. That list is the whole contract the lead speaks to the
// operator through, and a field "for the operator to answer" would have to be added to it.
// open_for_user is the one retired field: orchestrator/v1 is under buf breaking, which refuses a
// deleted field, so it stays until the next package version, deprecated and hidden from the tool.
// The test requires both, so the lead cannot send it and no client is invited to show it.
func TestReportHasNoFieldThatAsksTheOperator(t *testing.T) {
	declared := []string{"headline", "changed", "in_flight", "decisions", "kind", "sequence", "scope"}
	fields := (&pb.Report{}).ProtoReflect().Descriptor().Fields()
	var got []string
	for i := 0; i < fields.Len(); i++ {
		field := fields.Get(i)
		if field.Name() == "open_for_user" {
			deprecated := field.Options().(*descriptorpb.FieldOptions).GetDeprecated()
			hidden := proto.GetExtension(field.Options(), pb.E_ToolFieldHidden).(bool)
			if !deprecated || !hidden {
				t.Errorf("open_for_user must stay deprecated and hidden from the tool; deprecated=%v hidden=%v", deprecated, hidden)
			}
			continue
		}
		got = append(got, string(field.Name()))
	}
	slices.Sort(got)
	want := slices.Clone(declared)
	slices.Sort(want)
	if !slices.Equal(got, want) {
		t.Fatalf("Report has fields %v, the rule allows %v: a field must report, never hand the operator a decision; if the new field only reports, add it to this list", got, want)
	}
}

// handsToOperator matches the ways a lead is told to pass a decision to the operator: to ask,
// to wait on, or to leave something open for them. It is a tripwire for the wording this rule
// removed, not a proof; the structural test above is what bounds the contract. An operator
// pressing Catch me up, or waiting for the final report, is the operator acting, and does not match.
var handsToOperator = regexp.MustCompile(`(?i)\b(open for the (user|operator)|needs? (you|the operator|the user)|ask(ing)? (the )?(operator|user)|(wait|waiting) (for|on) (the )?(operator|user)\b[^.]*\b(answer|decision|reply|input|choice)|leave[^.]*to the (operator|user) to (decide|choose))`)

// Every word the lead is sent, and every word of the tool it reports through, is free of that
// wording. The templates are all of the text this package renders for an agent; the tool text is
// the post_report description and its fields' docs, read from the contract.
func TestLeadPromptsNeverHandADecisionToTheOperator(t *testing.T) {
	paths, err := filepath.Glob(filepath.Join("templates", "*.tmpl"))
	if err != nil || len(paths) == 0 {
		t.Fatalf("no templates: %v", err)
	}
	for _, path := range paths {
		text, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if m := handsToOperator.FindString(string(text)); m != "" {
			t.Errorf("%s says %q: the lead decides and records it under decisions", path, m)
		}
	}
	d := (&pb.Report{}).ProtoReflect().Descriptor()
	texts := []string{proto.GetExtension(d.Options(), pb.E_McpTool).(*pb.McpTool).GetDescription()}
	for i := 0; i < d.Fields().Len(); i++ {
		texts = append(texts, proto.GetExtension(d.Fields().Get(i).Options(), pb.E_ToolFieldDoc).(string))
	}
	for _, text := range texts {
		if m := handsToOperator.FindString(text); m != "" {
			t.Errorf("a post_report doc says %q: the lead decides and records it under decisions", m)
		}
	}
}

// The prompts a lead starts from, its brief and the handover that opens a replacement lead, state the
// rule themselves, so the check above is not satisfied by silence. They are read as rendered, since the
// words are shared by a template both include.
func TestTheLeadsFirstPromptsStateTheLeadDecides(t *testing.T) {
	leadPrompts := map[string]core.MessageBody{
		"lead brief":    core.LeadBrief{Task: "kv-store", FanoutLimit: 2},
		"lead handover": core.LeadHandover{Task: "kv-store", FanoutLimit: 2, Previous: core.AgentSpec{Harness: core.Codex}},
	}
	for name, body := range leadPrompts {
		text, err := prompts.Render(core.Message{ID: "m1", Body: body})
		if err != nil {
			t.Fatal(err)
		}
		for _, want := range []string{"decide every open question yourself", "never hand a decision to the operator", "external factor you cannot fix"} {
			if !strings.Contains(text, want) {
				t.Errorf("%s lacks %q", name, want)
			}
		}
	}
}
