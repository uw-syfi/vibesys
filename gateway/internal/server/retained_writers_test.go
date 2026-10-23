package server_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/proto"
)

// Saved logs are produced by preceding Python writers, never the current writer.
// Each directory in the retained corpus runs through checkpoints, replay and Tail.
func TestSavedPreviousWritersRetainInputFacts(t *testing.T) {
	sources, err := os.ReadDir("../../../proto/testdata/retained")
	if err != nil {
		t.Fatal(err)
	}
	manifest, err := os.ReadFile("../../../proto/testdata/retained/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases map[string]struct {
		Input          float64 `json:"input"`
		MeasurementSeq uint64  `json:"measurement_seq"`
	}
	if err := json.Unmarshal(manifest, &cases); err != nil {
		t.Fatal(err)
	}
	for _, source := range sources {
		if !source.IsDir() {
			continue
		}
		want, ok := cases[source.Name()]
		if !ok {
			t.Fatalf("retained writer %s missing from manifest", source.Name())
		}
		for _, checkpoint := range []bool{false, true} {
			name := source.Name() + "/records"
			if checkpoint {
				name = source.Name() + "/keyframe"
			}
			t.Run(name, func(t *testing.T) {
				f := start(t)
				directory := filepath.Join("../../../proto/testdata/retained", source.Name())
				for _, name := range []string{"run.log", "run.head"} {
					data, err := os.ReadFile(filepath.Join(directory, name))
					if err != nil {
						t.Fatal(err)
					}
					write(t, filepath.Join(f.run, name), false, data)
				}
				if checkpoint {
					entries, err := os.ReadDir(filepath.Join(directory, "keyframes"))
					if err != nil {
						t.Fatal(err)
					}
					if err := os.Mkdir(filepath.Join(f.run, "keyframes"), 0700); err != nil {
						t.Fatal(err)
					}
					for _, entry := range entries {
						data, err := os.ReadFile(filepath.Join(directory, "keyframes", entry.Name()))
						if err != nil {
							t.Fatal(err)
						}
						write(t, filepath.Join(f.run, "keyframes", entry.Name()), false, data)
					}
				}
				c := client(t, f, false)
				response, err := c.Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
				if err != nil {
					t.Fatal(err)
				}
				var epochs []*storev1.EpochVersion
				if len(response.Keyframe) > 0 {
					state := restored(t, response, nil)
					epochs = state.Epochs
				}
				for _, payload := range response.Records {
					record := &storev1.Record{}
					if err := proto.Unmarshal(payload, record); err != nil {
						t.Fatal(err)
					}
					if fact := retainedRecordFact(record); fact != nil {
						if record.Seq != want.MeasurementSeq {
							t.Fatalf("input fact at sequence %d, want only at its measurement %d", record.Seq, want.MeasurementSeq)
						}
						epochs = append(epochs, &storev1.EpochVersion{Seq: record.Seq, Epoch: &storev1.EpochStarted{Run: &storev1.RunDescription{InputMeasurement: fact}}})
					}
				}
				assertRetainedInput(t, epochs, want.Input, want.MeasurementSeq)
				if !checkpoint {
					pinRetainedResponse(t, directory, response)
				}
				watch, err := c.WatchRuns(f.ctx, &apiv1.WatchRunsRequest{})
				if err != nil {
					t.Fatal(err)
				}
				observed := watchEntry(t, watch)
				if observed.GetHead().GetObjective().GetInputValue() != want.Input {
					t.Fatalf("retained head lost input: %v", observed)
				}
				tail, err := c.Tail(f.ctx, &apiv1.TailRequest{RunId: "run-1"})
				if err != nil {
					t.Fatal(err)
				}
				batch, err := tail.Recv()
				if err != nil {
					t.Fatal(err)
				}
				found := false
				for _, payload := range batch.Records {
					record := &storev1.Record{}
					if err := proto.Unmarshal(payload, record); err != nil {
						t.Fatal(err)
					}
					if retainedRecordFact(record).GetMeasured().GetValue() == want.Input {
						found = true
					}
				}
				if !found {
					t.Fatal("Tail omitted the retained input declaration")
				}
			})
		}
	}
}

func assertRetainedInput(t *testing.T, epochs []*storev1.EpochVersion, value float64, seq uint64) {
	t.Helper()
	found := false
	for _, version := range epochs {
		fact := version.GetEpoch().GetRun().GetInputMeasurement()
		if fact != nil && version.Seq < seq {
			t.Fatalf("input is visible at sequence %d, before its recorded measurement at %d", version.Seq, seq)
		}
		if fact.GetMeasured().GetValue() == value {
			found = true
		}
	}
	if !found {
		t.Fatal("retained input fact disappeared")
	}
}

// This exact public RPC reply is also consumed by the screen projection regression.
func pinRetainedResponse(t *testing.T, directory string, response *apiv1.SkeletonResponse) {
	t.Helper()
	data, err := proto.MarshalOptions{Deterministic: true}.Marshal(response)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(directory, "skeleton.response.pb")
	if os.Getenv("VIBESYS_UPDATE_GOLDEN") != "" {
		if err := os.WriteFile(path, data, 0644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(want) != string(data) {
		t.Fatal("retained RPC response changed; rerun with VIBESYS_UPDATE_GOLDEN=1")
	}
}

func retainedRecordFact(record *storev1.Record) *storev1.InputMeasurement {
	field := record.ProtoReflect().Descriptor().Fields().ByName("input_measurement")
	if field == nil || !record.ProtoReflect().Has(field) {
		return nil
	}
	return record.ProtoReflect().Get(field).Message().Interface().(*storev1.InputMeasurement)
}
