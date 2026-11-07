package prompts_test

import (
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// VibeSys is fully autonomous: the lead decides every open question itself, records it under
// decisions and goes on. Only a hard external block (no disk space, no quota, a host that is gone)
// stops a run, and it shows as the run's ended or blocked state with its cause, never as a question.
// These tests hold that rule in two parts, and neither is a word list over all prose.
//
// The structural part: the fields of Report are a closed list, so a new field fails here until
// someone reads it against the rule. That list is the whole contract the lead speaks to the
// operator through, and a field "for the operator to answer" would have to be added to it.
func TestReportHasNoFieldThatAsksTheOperator(t *testing.T) {
	declared := []string{"headline", "changed", "in_flight", "decisions", "kind", "sequence", "scope"}
	fields := (&pb.Report{}).ProtoReflect().Descriptor().Fields()
	var got []string
	for i := 0; i < fields.Len(); i++ {
		got = append(got, string(fields.Get(i).Name()))
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

// The lead brief states the rule itself, so the check above is not satisfied by silence.
func TestLeadBriefStatesTheLeadDecides(t *testing.T) {
	text, err := os.ReadFile(filepath.Join("templates", "lead_brief.md.tmpl"))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"decide every open question yourself", "never hand a decision to the operator", "external factor you cannot fix"} {
		if !strings.Contains(string(text), want) {
			t.Errorf("lead brief lacks %q", want)
		}
	}
}
