package subscription_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"os"
	"slices"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	"buf.build/go/protovalidate"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/clock"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription/subscriptiontest"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// The frame corpus is what the real hub sends for scripted producers, written down so that the
// TypeScript fold reads exactly it. Each stream lists its frames in order, and the producer's own
// map when its script was done, which the client's fold must equal. Run this test with
// -update-corpus after a change to the hub, and review the diff.
var updateCorpus = flag.Bool("update-corpus", false, "rewrite the subscription frame corpus")

const corpusPath = "../../../tests/conformance/fixtures/subscription/frames.json"

// corpusMaxKeys is the key bound of the corpus's topic, for the fold under test.
const corpusMaxKeys = 4

type corpusFile struct {
	Provenance string       `json:"provenance"`
	MaxKeys    int          `json:"max_keys"`
	Cases      []corpusCase `json:"cases"`
}

type corpusCase struct {
	Name    string         `json:"name"`
	Streams []corpusStream `json:"streams"`
}

type corpusStream struct {
	Frames []json.RawMessage `json:"frames"`
	// Producer is the producer's map by key after its last step, as protojson MachineStats.
	Producer map[string]json.RawMessage `json:"producer"`
	// Ends is "open" for a stream the client left, and "failed" for one that ended in a failed frame.
	Ends string `json:"ends"`
}

// producer records what a source holds as it writes to the hub, so that the corpus names the
// producer's map independently of the frames the hub derived from it.
type producer struct {
	sink  subscription.Sink[stats]
	state map[string]stats
}

func (p *producer) put(key string, second int64) {
	message := value(second)
	p.state[key] = message
	p.sink.Put(key, message)
}

func (p *producer) remove(key string) {
	delete(p.state, key)
	p.sink.Remove(key)
}

func (p *producer) synced() { p.sink.Synced() }

type step func(*producer)

// streamScript is one producer run: its steps, each taken with the hub flushed before the next,
// then either silence for `quiet` or a failure.
type streamScript struct {
	steps []step
	quiet time.Duration
	fail  bool
}

// record drives each script through its own stream of one hub, in order, as a client that
// reconnects after each would see them.
func record(t *testing.T, scripts []streamScript) []corpusStream {
	t.Helper()
	var streams []corpusStream
	synctest.Test(t, func(t *testing.T) {
		var current streamScript
		var held *producer
		gate := make(chan struct{})
		source := script(func(ctx context.Context, sink subscription.Sink[stats]) error {
			own := &producer{sink: sink, state: map[string]stats{}}
			held = own
			for _, each := range current.steps {
				select {
				case <-gate:
					each(own)
				case <-ctx.Done():
					return ctx.Err()
				}
			}
			if current.fail {
				// The failure comes after the last step has been flushed to the client.
				select {
				case <-gate:
					return errors.New("scripted failure")
				case <-ctx.Done():
					return ctx.Err()
				}
			}
			return wait(ctx)
		})
		// Flushes come on the interval's ticks, which fake time makes exact, so what a step writes
		// together is sent together. Without it a sender could wake between two writes.
		policy := policy
		policy.MaxKeys = corpusMaxKeys
		policy.MinInterval = time.Second
		built := topic(source)
		built.Policy = policy
		hub := subscription.New(clock.System{}, built)
		for _, each := range scripts {
			current = each
			stream := subscriptiontest.Open(hub, 1, request)
			synctest.Wait()
			turns := len(each.steps)
			if each.fail {
				turns++
			}
			for range turns {
				gate <- struct{}{}
				synctest.Wait()
				time.Sleep(time.Second)
				synctest.Wait()
			}
			if each.quiet > 0 {
				time.Sleep(each.quiet)
				synctest.Wait()
			}
			streams = append(streams, corpusStreamOf(t, stream, held, each))
			stream.Stop()
			synctest.Wait()
		}
	})
	return streams
}

func corpusStreamOf(t *testing.T, stream *subscriptiontest.Stream, held *producer, each streamScript) corpusStream {
	t.Helper()
	out := corpusStream{Producer: map[string]json.RawMessage{}, Ends: "open"}
	for _, frame := range inBatchesByKey(stream.Frames()) {
		if err := protovalidate.Validate(frame); err != nil {
			t.Fatalf("the hub sent a frame its own schema refuses: %v", err)
		}
		out.Frames = append(out.Frames, canonical(t, frame))
	}
	for key, message := range held.state {
		out.Producer[key] = canonical(t, message)
	}
	if each.fail {
		if _, ended := stream.Ended(); !ended {
			t.Fatal("a failed topic did not end its stream")
		}
		out.Ends = "failed"
	}
	return out
}

// keyOf names the key a frame is about, or "" for a frame about the stream.
func keyOf(frame *apiv1.SubscribeResponse) string {
	switch body := frame.GetFrame().(type) {
	case *apiv1.SubscribeResponse_Entry:
		return body.Entry.GetKey()
	case *apiv1.SubscribeResponse_Removed:
		return body.Removed
	default:
		return ""
	}
}

// inBatchesByKey puts the entries and removals between two frames about the stream in key order.
// The hub promises their order only by kind and iterates its keys as Go maps do, so the order of
// keys within a batch is no part of what a client may rely on and would make the corpus flap.
func inBatchesByKey(frames []*apiv1.SubscribeResponse) []*apiv1.SubscribeResponse {
	out := slices.Clone(frames)
	for start := 0; start < len(out); {
		end := start
		for end < len(out) && keyOf(out[end]) != "" {
			end++
		}
		slices.SortStableFunc(out[start:end], func(a, b *apiv1.SubscribeResponse) int { return strings.Compare(keyOf(a), keyOf(b)) })
		start = end + 1
	}
	return out
}

// canonical is protojson with a fixed layout, which protojson itself does not promise.
func canonical(t *testing.T, message proto.Message) json.RawMessage {
	t.Helper()
	raw, err := protojson.Marshal(message)
	if err != nil {
		t.Fatal(err)
	}
	var tree any
	if err := json.Unmarshal(raw, &tree); err != nil {
		t.Fatal(err)
	}
	fixed, err := json.Marshal(tree)
	if err != nil {
		t.Fatal(err)
	}
	return fixed
}

func TestFrameCorpus(t *testing.T) {
	cases := []struct {
		name    string
		scripts []streamScript
	}{
		{"a snapshot of two keys", []streamScript{{steps: []step{
			func(p *producer) { p.put("a", 1); p.put("b", 2); p.synced() },
		}}}},
		{"changes after the snapshot", []streamScript{{steps: []step{
			func(p *producer) { p.put("a", 1); p.put("b", 2); p.synced() },
			func(p *producer) { p.put("a", 3); p.remove("b"); p.put("c", 4) },
		}}}},
		{"an empty snapshot", []streamScript{{steps: []step{
			func(p *producer) { p.synced() },
		}}}},
		{"a key removed before it was sent is never sent", []streamScript{{steps: []step{
			func(p *producer) { p.put("a", 1); p.put("b", 2); p.remove("b"); p.synced() },
		}}}},
		{"writes between flushes are conflated to the latest value", []streamScript{{steps: []step{
			func(p *producer) { p.put("a", 1); p.synced() },
			func(p *producer) { p.put("a", 2); p.put("a", 3); p.put("a", 4) },
		}}}},
		{"a new stream starts from the producer's map, not the last stream's", []streamScript{
			{steps: []step{func(p *producer) { p.put("a", 1); p.put("b", 2); p.synced() }}},
			{steps: []step{func(p *producer) { p.put("a", 5); p.synced() }}},
		}},
		{"heartbeats keep a quiet stream alive", []streamScript{{steps: []step{
			func(p *producer) { p.put("a", 1); p.synced() },
		}, quiet: 12 * time.Second}}},
		{"a failing topic ends its stream in a failed frame", []streamScript{{steps: []step{
			func(p *producer) { p.put("a", 1); p.synced() },
		}, fail: true}}},
	}
	got := corpusFile{
		Provenance: "Emitted by gateway/internal/subscription/corpus_test.go from the real hub. " +
			"Run `go test ./internal/subscription -run TestFrameCorpus -update-corpus` to rewrite it. " +
			"@vibesys/subscription folds every stream in a test.",
		MaxKeys: corpusMaxKeys,
	}
	for _, each := range cases {
		got.Cases = append(got.Cases, corpusCase{Name: each.name, Streams: record(t, each.scripts)})
	}
	data, err := json.MarshalIndent(got, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	data = append(data, '\n')
	if *updateCorpus {
		if err := os.WriteFile(corpusPath, data, 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(corpusPath)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(want, data) {
		t.Fatal("the hub no longer sends the frames in the corpus; run with -update-corpus and review the diff")
	}
}
