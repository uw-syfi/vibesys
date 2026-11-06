//go:build !race

package wire_test

import (
	"math/rand/v2"
	"reflect"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// A reader that applies the messages of a stream holds, at every message boundary, the task as it
// was at some look: a reset in flight changes nothing it holds, and a message of a stream cut in the
// middle of a reset leaves it, and its token, where they were. After a cut the stream starts again
// with a reset.
func TestAReaderNeverHoldsAPartialResetWhereverTheStreamIsCut(t *testing.T) {
	for seed := range uint64(300) {
		r := rand.New(rand.NewPCG(seed, 5))
		looks := []map[string]string{{}}
		rd := reader{view: map[string]string{}}
		var sent core.Sent
		cutAt := r.IntN(40)
		sinceCut := 0
		for step := 0; step < 12; step++ {
			// A task that grows past one message makes a reset of several.
			v := manyJobs(r, core.MaxFactsPerMessage*2+10)
			looks = append(looks, viewOf(v))
			var send core.Send
			send, sent = core.PlanSend(sent, v)
			for _, m := range wire.TaskMessages(send) {
				m.Sequence = rd.last + 1
				sinceCut++
				if sinceCut == cutAt {
					// The stream is cut: what is pending is dropped and the next stream resets.
					before, token := rd.view, rd.token
					rd.pending, rd.resetting = nil, false
					if !reflect.DeepEqual(rd.view, before) || rd.token != token {
						t.Fatalf("seed %d: a cut changed the view or the token", seed)
					}
					rd = reader{view: rd.view, token: rd.token}
					sent = nil
					sinceCut = 0
					send, sent = core.PlanSend(sent, v)
					msgs := wire.TaskMessages(send)
					for i, m2 := range msgs {
						m2.Sequence = uint64(i + 1)
						if err := rd.apply(m2); err != nil {
							t.Fatalf("seed %d: %v", seed, err)
						}
						rd.assertSome(t, seed, looks)
					}
					break
				}
				if err := rd.apply(m); err != nil {
					t.Fatalf("seed %d step %d: %v", seed, step, err)
				}
				rd.assertSome(t, seed, looks)
			}
		}
	}
}
