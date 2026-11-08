package production

import (
	"context"
	"errors"
	"math/rand/v2"
	"reflect"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// fakeDescriber is the CLI daemon's DescribeTask, answering what a test set and remembering the request.
type fakeDescriber struct {
	resp *cliv1.DescribeTaskResponse
	err  error
	got  *cliv1.DescribeTaskRequest
}

func (f *fakeDescriber) DescribeTask(_ context.Context, r *cliv1.DescribeTaskRequest) (*cliv1.DescribeTaskResponse, error) {
	f.got = r
	return f.resp, f.err
}

func cardResponse(c *cliv1.TaskCard) *cliv1.DescribeTaskResponse {
	return &cliv1.DescribeTaskResponse{Outcome: &cliv1.DescribeTaskResponse_Card{Card: c}}
}

// Every valid card the daemon answers is read as the core's card of the same facts, whatever mix of
// fields it has, and the daemon is asked for the committed task in the repository named.
func TestTheTaskReaderReadsEveryValidCardAsTheCoreHoldsIt(t *testing.T) {
	r := rand.New(rand.NewPCG(4, 4))
	for i := range 300 {
		task := "t" + someWord(r, 10)
		card := &cliv1.TaskCard{Task: task, Heading: someWord(r, 30), HardwareName: someWord(r, 20)}
		want := core.TaskCard{Task: task, Heading: card.Heading, HardwareName: card.HardwareName}
		if r.IntN(2) == 0 {
			card.Domain = cliv1.TaskDomain_TASK_DOMAIN_LLM_SERVING
			want.Domain = core.DomainLLMServing
			card.Hardware = &cliv1.AcceleratorNeed{Vendor: cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, Model: cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300X, Count: uint32(1 + r.IntN(8))}
			want.Hardware = &core.AcceleratorNeed{Vendor: core.VendorAMD, Model: core.ModelMI300X, Count: card.Hardware.Count}
		}
		for range r.IntN(4) {
			name := "m" + someWord(r, 8)
			card.Objectives = append(card.Objectives, &cliv1.CardObjective{Name: name, Direction: cliv1.TaskObjectiveDirection_TASK_OBJECTIVE_DIRECTION_MIN, Unit: "ms"})
			want.Objectives = append(want.Objectives, core.CardObjective{Name: name, Direction: core.Minimize, Unit: "ms"})
		}
		d := &fakeDescriber{resp: cardResponse(card)}
		repo := "/" + someWord(r, 12) + "x"
		got, err := TaskReader{d}.Read(context.Background(), daemon.TaskRef{Source: daemon.TaskCommitted, Repository: repo, Task: task})
		if err != nil || !reflect.DeepEqual(got, want) {
			t.Fatalf("round %d: read %+v, %v; want %+v", i, got, err, want)
		}
		if c := d.got.GetCommitted(); c.GetRepository() != repo || c.GetTask() != task || c.Revision != nil {
			t.Fatalf("round %d: asked %v", i, d.got)
		}
	}
}

// A task the daemon cannot read is the typed error with its reason and its words, a failed call and an
// answer that breaks the contract or is for another task are plain errors, and none is a card.
func TestTheTaskReaderRefusesWhatTheContractDoesNotAllow(t *testing.T) {
	ref := daemon.TaskRef{Source: daemon.TaskCommitted, Repository: "/repo", Task: "t"}
	for reason, want := range unreadableReasons {
		d := &fakeDescriber{resp: &cliv1.DescribeTaskResponse{Outcome: &cliv1.DescribeTaskResponse_Unreadable{Unreadable: &cliv1.TaskUnreadable{Reason: reason, Detail: "objectives.toml: unknown key"}}}}
		var unreadable *daemon.TaskUnreadable
		if _, err := (TaskReader{d}).Read(context.Background(), ref); !errors.As(err, &unreadable) || unreadable.Reason != want || !strings.Contains(unreadable.Detail, "unknown key") {
			t.Fatalf("%v: got %v", reason, err)
		}
	}
	bad := map[string]*fakeDescriber{
		"call failed":       {err: errors.New("socket closed")},
		"another task":      {resp: cardResponse(&cliv1.TaskCard{Task: "other"})},
		"empty task":        {resp: cardResponse(&cliv1.TaskCard{})},
		"direction unset":   {resp: cardResponse(&cliv1.TaskCard{Task: "t", Objectives: []*cliv1.CardObjective{{Name: "x"}}})},
		"count out":         {resp: cardResponse(&cliv1.TaskCard{Task: "t", Hardware: &cliv1.AcceleratorNeed{Vendor: cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, Count: 0}})},
		"no outcome":        {resp: &cliv1.DescribeTaskResponse{}},
		"unreadable no why": {resp: &cliv1.DescribeTaskResponse{Outcome: &cliv1.DescribeTaskResponse_Unreadable{Unreadable: &cliv1.TaskUnreadable{}}}},
	}
	for name, d := range bad {
		var unreadable *daemon.TaskUnreadable
		if card, err := (TaskReader{d}).Read(context.Background(), ref); err == nil || errors.As(err, &unreadable) {
			t.Errorf("%s: got %+v, %v, want a plain error", name, card, err)
		}
	}
}

// The job tool's word that it cannot run a task crosses the reader whole, whatever else the card says:
// the reader reads it into the card and decides nothing, so the daemon, which decides what a start does
// with it, sees exactly what the job tool said.
func TestTheTaskReaderCarriesTheJobToolsReasonForATaskItCannotRun(t *testing.T) {
	r := rand.New(rand.NewPCG(8, 9))
	for i := range 200 {
		task := "t" + someWord(r, 10)
		card := &cliv1.TaskCard{Task: task, Heading: someWord(r, 30), HardwareName: someWord(r, 20), Unsupported: "vibesys.input.toml: " + someWord(r, 100)}
		if r.IntN(2) == 0 {
			card.Domain = cliv1.TaskDomain_TASK_DOMAIN_DATA_STRUCTURES
			card.Hardware = &cliv1.AcceleratorNeed{Vendor: cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_CPU, Count: 1}
		}
		got, err := TaskReader{&fakeDescriber{resp: cardResponse(card)}}.Read(context.Background(), daemon.TaskRef{Source: daemon.TaskCommitted, Repository: "/repo", Task: task})
		if err != nil || got.Unsupported != card.Unsupported || got.Task != task {
			t.Fatalf("round %d: read %+v, %v; want the card saying %q", i, got, err, card.Unsupported)
		}
	}
}

// A card with a field this build does not know is refused, not read with the field dropped.
func TestTheTaskReaderRefusesACardWithAnUnknownField(t *testing.T) {
	card := &cliv1.TaskCard{Task: "t"}
	card.ProtoReflect().SetUnknown([]byte{0xf8, 0x7f, 0x01}) // field 2047, varint 1
	d := &fakeDescriber{resp: cardResponse(card)}
	if _, err := (TaskReader{d}).Read(context.Background(), daemon.TaskRef{Source: daemon.TaskCommitted, Repository: "/repo", Task: "t"}); err == nil {
		t.Fatal("a card with an unknown field was read")
	}
}

// Each source of a reference is the request's matching source, and the revision is passed only when
// one is named.
func TestTheTaskReaderAsksForTheSourceTheReferenceNames(t *testing.T) {
	ok := cardResponse(&cliv1.TaskCard{Task: "t"})
	for _, c := range []struct {
		ref  daemon.TaskRef
		want proto.Message
	}{
		{daemon.TaskRef{Source: daemon.TaskCommitted, Repository: "/r", Revision: "abc", Task: "t"}, &cliv1.CommittedTask{Repository: "/r", Revision: proto.String("abc"), Task: "t"}},
		{daemon.TaskRef{Source: daemon.TaskWorkingTree, Repository: "/r", Task: "t"}, &cliv1.WorkingTreeTask{Repository: "/r", Task: "t"}},
		{daemon.TaskRef{Source: daemon.TaskFolder, Repository: "/r", Task: "t"}, &cliv1.FolderTask{Root: "/r", Task: "t"}},
	} {
		d := &fakeDescriber{resp: ok}
		if _, err := (TaskReader{d}).Read(context.Background(), c.ref); err != nil {
			t.Fatal(err)
		}
		var got proto.Message
		switch s := d.got.GetSource().(type) {
		case *cliv1.DescribeTaskRequest_Committed:
			got = s.Committed
		case *cliv1.DescribeTaskRequest_WorkingTree:
			got = s.WorkingTree
		case *cliv1.DescribeTaskRequest_Folder:
			got = s.Folder
		}
		if !proto.Equal(got, c.want) {
			t.Errorf("%+v asked %v, want %v", c.ref, got, c.want)
		}
	}
	if _, err := (TaskReader{&fakeDescriber{}}).Read(context.Background(), daemon.TaskRef{Task: "t"}); err == nil {
		t.Error("a reference with no source was read")
	}
}
