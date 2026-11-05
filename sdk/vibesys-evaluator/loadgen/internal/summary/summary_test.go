package summary_test

import (
	"math/rand/v2"
	"testing"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/summary"
)

const ms = int64(1_000_000)

var window = summary.Window{Start: 1000 * ms, End: 2000 * ms}

func randomRecords(random *rand.Rand) []summary.Record {
	var records []summary.Record
	for i := range uint64(random.IntN(300)) {
		scheduled := 500*ms + random.Int64N(1600*ms)
		picked := scheduled + random.Int64N(5*ms)
		record := summary.Record{Picked: picked, Result: &pb.OperationResult{
			Index: i, Operation: random.Uint32N(3), ScheduledUnixNanos: scheduled,
			StartUnixNanos: picked + random.Int64N(2*ms), Error: pb.ErrorKind(random.IntN(2) * (1 + random.IntN(5))),
		}}
		if random.IntN(20) == 0 { // never sent
			record.Picked, record.Result.StartUnixNanos = 0, 0
			record.Result.Error = pb.ErrorKind_ERROR_KIND_CANCELLED
		}
		record.Result.EndUnixNanos = max(record.Result.GetStartUnixNanos(), scheduled) + random.Int64N(300*ms)
		records = append(records, record)
	}
	return records
}

func summarize(t *testing.T, records []summary.Record) *pb.PhaseSummary {
	t.Helper()
	agg := summary.New(window, []string{"a", "b", "c"})
	for _, record := range records {
		agg.Add(record)
	}
	s, err := agg.Summary(summary.Phase{Model: pb.LoadModel_LOAD_MODEL_OPEN, TargetRate: 100, MinSustained: 0.5})
	if err != nil {
		t.Fatal(err)
	}
	return s
}

// Property: the summary does not depend on the order results arrive in, and its counts are the
// window's operations, split as the schema defines them.
func TestSummaryIsOrderFreeAndCountsTheWindow(t *testing.T) {
	for seed := range uint64(60) {
		random := rand.New(rand.NewPCG(seed, 5))
		records := randomRecords(random)
		got := summarize(t, records)
		random.Shuffle(len(records), func(i, j int) { records[i], records[j] = records[j], records[i] })
		if again := summarize(t, records); !proto.Equal(got, again) {
			t.Fatalf("seed %d: order changed the summary", seed)
		}
		var completed, sent, errs, spilled, ended, started, ok uint64
		for _, r := range records {
			res := r.Result
			if res.GetStartUnixNanos() != 0 && window.Contains(res.GetStartUnixNanos()) {
				started++
			}
			if !window.Contains(res.GetScheduledUnixNanos()) {
				continue
			}
			completed++
			if res.GetError() != pb.ErrorKind_ERROR_KIND_UNSPECIFIED {
				errs++
			} else {
				ok++
			}
			if res.GetStartUnixNanos() == 0 {
				continue
			}
			sent++
			if res.GetEndUnixNanos() >= window.End {
				spilled++
			} else {
				ended++
			}
		}
		if got.GetCompleted() != completed || got.GetSent() != sent || got.GetErrors() != errs || got.GetSpilled() != spilled {
			t.Fatalf("seed %d: counts %v, want completed %d sent %d errors %d spilled %d", seed, got, completed, sent, errs, spilled)
		}
		if got.GetAchievedRate() != float64(ended)/window.Secs() || got.GetOfferedRate() != float64(started)/window.Secs() {
			t.Fatalf("seed %d: rates %v %v", seed, got.GetAchievedRate(), got.GetOfferedRate())
		}
		var perOp uint64
		for _, op := range got.GetOperations() {
			perOp += op.GetCompleted()
		}
		latency, err := summary.Decode(got.GetLatency())
		if err != nil {
			t.Fatal(err)
		}
		if perOp != completed || uint64(latency.TotalCount()) != ok {
			t.Fatalf("seed %d: per-op %d, latency count %d, want %d and %d", seed, perOp, latency.TotalCount(), completed, ok)
		}
		if got.GetSustained() != (got.GetOfferedRate() >= 50) {
			t.Fatalf("seed %d: sustained %v at offered %v", seed, got.GetSustained(), got.GetOfferedRate())
		}
	}
}

// Latency is measured from the scheduled time, so a late start counts in full; send lag and the
// exclusive-key wait are apart.
func TestLatencyCountsFromScheduledTime(t *testing.T) {
	scheduled := 1500 * ms
	got := summarize(t, []summary.Record{{Picked: scheduled + 300*ms, Result: &pb.OperationResult{
		ScheduledUnixNanos: scheduled, StartUnixNanos: scheduled + 400*ms, EndUnixNanos: scheduled + 410*ms,
	}}})
	check := func(name string, h *pb.HdrHistogram, wantMicros int64) {
		value, err := summary.Percentile(h, 100)
		if err != nil {
			t.Fatal(err)
		}
		if value < wantMicros*999/1000 || value > wantMicros*1001/1000 {
			t.Fatalf("%s: %d us, want %d", name, value, wantMicros)
		}
	}
	check("latency", got.GetLatency(), 410_000)
	check("send lag", got.GetSendLag(), 300_000)
	check("exclusive wait", got.GetExclusiveWait(), 100_000)
	if got.GetMaxSendLagMicros() != 300_000 {
		t.Fatalf("max send lag %d", got.GetMaxSendLagMicros())
	}
}

func TestHistogramEncodingRoundTrips(t *testing.T) {
	got := summarize(t, randomRecords(rand.New(rand.NewPCG(9, 9))))
	for _, h := range []*pb.HdrHistogram{got.GetLatency(), got.GetSendLag()} {
		decoded, err := summary.Decode(h)
		if err != nil {
			t.Fatal(err)
		}
		again, err := summary.Encode(decoded)
		if err != nil {
			t.Fatal(err)
		}
		back, _ := summary.Decode(again)
		if !back.Equals(decoded) || h.GetEncoded()[0] != 0x1c {
			t.Fatal("histogram did not round-trip or is not the V2 compressed encoding")
		}
	}
}
