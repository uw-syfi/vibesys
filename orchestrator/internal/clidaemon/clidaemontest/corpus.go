package clidaemontest

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"

	"io"

	"reflect"
	"strings"
	"testing"

	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// Scenario is the shared, callee-owned transcript format. Unknown fields and
// operations fail rather than silently omitting a new corpus scenario.
type Scenario struct {
	Lifecycle     bool                `json:"lifecycle"`
	DuringReset   []Operation         `json:"during_reset"`
	AfterMessages int                 `json:"after_messages"`
	Description   string              `json:"description"`
	JudgedCommits uint32              `json:"judged_commits"`
	Setup         []Operation         `json:"setup"`
	Ops           []Operation         `json:"ops"`
	Cuts          []int               `json:"cuts"`
	Expect        [][]json.RawMessage `json:"expect"`
}
type Operation struct {
	Run     string `json:"run,omitempty"`
	Agent   string `json:"agent,omitempty"`
	Job     string `json:"job,omitempty"`
	Op      string `json:"op"`
	Commit  string `json:"commit,omitempty"`
	Samples string `json:"samples,omitempty"`
	Title   string `json:"title,omitempty"`
	Body    string `json:"body,omitempty"`
	Number  uint32 `json:"number,omitempty"`
	Text    string `json:"text,omitempty"`
}

// Corpus replays operations through the Fake, compares every wire message,
// and checks Session at every boundary, including cuts inside resets.
func Corpus(t *testing.T, raw []byte) {
	t.Helper()
	// Commit labels are normalized by the corpus, never by product code.
	for n := 0; n < 3; n++ {
		raw = []byte(strings.ReplaceAll(string(raw), fmt.Sprintf("\"c%d\"", n), fmt.Sprintf("\"%s\"", commitName(n))))
	}
	var sc Scenario
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&sc); err != nil {
		t.Fatal(err)
	}
	f := New()
	model := newModel()
	for _, op := range sc.Setup {
		model.operation(t, op, sc.Lifecycle)
	}
	f.SetFacts(model.facts())
	var session clidaemon.Session
	request := &pb.WatchTaskRequest{Context: &pb.CallContext{Repository: "/repo", Task: "task"}, JudgedCommits: sc.JudgedCommits}
	index := 0
	during := false
	for streamIndex, want := range sc.Expect {
		if sc.Lifecycle && streamIndex > len(sc.Cuts) {
			model.lifecycle(t, sc.Ops[index])
			index++
			f.SetFacts(model.facts())
		}
		token := session.Token()
		request.Epoch, request.AfterSequence = token.Epoch, token.Sequence
		stream, err := f.WatchTask(context.Background(), request)
		if err != nil {
			t.Fatal(err)
		}
		cut := streamIndex < len(sc.Cuts)
		for i, rawMessage := range want {
			m, err := stream.Recv()
			if err == io.EOF && !sc.Lifecycle && !cut && index < len(sc.Ops) {
				model.play(t, sc.Ops[index])
				index++
				f.SetFacts(model.facts())
				m, err = stream.Recv()
			}
			if err != nil {
				t.Fatalf("stream %d message %d: %v", streamIndex, i, err)
			}
			if !cut && !during && len(sc.DuringReset) > 0 && i+1 == sc.AfterMessages {
				during = true
				for _, op := range sc.DuringReset {
					model.operation(t, op, sc.Lifecycle)
				}
				f.SetFacts(model.facts())
			}
			expected := &pb.WatchTaskResponse{}
			normalized := strings.ReplaceAll(string(rawMessage), `"T"`, `"2026-01-01T00:00:00Z"`)
			if err := protojson.Unmarshal([]byte(normalized), expected); err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(m, expected) {
				t.Fatalf("stream %d message %d:\ngot %s\nwant %s", streamIndex, i, m, expected)
			}
			before := session.Facts()
			old := session.Token()
			changed, err := session.Apply(m)
			if err != nil {
				t.Fatal(err)
			}
			if m.GetCaughtUp() == nil && session.Token() != old {
				t.Fatal("token advanced before CaughtUp")
			}
			if !changed && !reflect.DeepEqual(before, session.Facts()) {
				t.Fatal("partial reset was published")
			}
		}
		if cut && len(want) != sc.Cuts[streamIndex] {
			t.Fatal("cut count does not match transcript")
		}
		session.Disconnected()
	}
	if index != len(sc.Ops) {
		t.Fatalf("played %d of %d operations", index, len(sc.Ops))
	}
	if !sameFacts(session.Facts(), model.facts()) {
		t.Fatal("final view differs from store fold")
	}
}

func sameFacts(a, b []*pb.TaskFact) bool {
	x, y := keyed(a), keyed(b)
	if len(x) != len(y) {
		return false
	}
	for k, v := range x {
		if !proto.Equal(v, y[k]) {
			return false
		}
	}
	return true
}
