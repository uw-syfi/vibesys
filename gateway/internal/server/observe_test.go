package server

import (
	"encoding/binary"
	"encoding/json"
	"hash/crc32"
	"math/rand"
	"os"
	"path/filepath"
	"testing"

	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/protobuf/proto"
)

const corpus = "../../../proto/testdata/retained"

type corpusCase struct {
	Input          float64 `json:"input"`
	MeasurementSeq uint64  `json:"measurement_seq"`
}

func corpusCases(t *testing.T) map[string]corpusCase {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(corpus, "manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	var cases map[string]corpusCase
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	return cases
}

func publish(t *testing.T, dir string, head *storev1.RunHead) {
	t.Helper()
	data, err := proto.Marshal(head)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "run.head"), data, 0600); err != nil {
		t.Fatal(err)
	}
}

// The log is read only for a head its writer left without the facts: a previous writer's.
// Whatever the generated sequence of reads, each committed prefix is scanned at most once,
// and every read states the facts its prefix measured.
func TestOnlyPreviousWriterHeadsScanTheirLogOncePerPrefix(t *testing.T) {
	for name, want := range corpusCases(t) {
		dir := t.TempDir()
		log, err := os.ReadFile(filepath.Join(corpus, name, "run.log"))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "run.log"), log, 0600); err != nil {
			t.Fatal(err)
		}
		stored, err := os.ReadFile(filepath.Join(corpus, name, "run.head"))
		if err != nil {
			t.Fatal(err)
		}
		base := &storev1.RunHead{}
		if err := proto.Unmarshal(stored, base); err != nil {
			t.Fatal(err)
		}
		last := base.GetCommittedSeq()
		type variant struct {
			committed       uint64
			stated, hasEpoc bool
		}
		for seed := int64(0); seed < 4; seed++ {
			rng := rand.New(rand.NewSource(seed))
			var observer headObserver
			scanned := map[variant]bool{}
			for range 100 {
				v := variant{rng.Uint64()%(last+1) + 1, rng.Intn(2) == 0, rng.Intn(2) == 0}
				if v.committed > last {
					v.committed = last
				}
				head := proto.Clone(base).(*storev1.RunHead)
				head.CommittedSeq = proto.Uint64(v.committed)
				head.Objective.InputValue = nil
				head.Objective.InputStated = v.stated
				head.PerformanceStated = true
				if !v.hasEpoc {
					head.Epoch = 0
				}
				publish(t, dir, head)
				got, err := observer.read(dir)
				if err != nil {
					t.Fatalf("%s %+v: %v", name, v, err)
				}
				if !v.stated || !v.hasEpoc {
					scanned[variant{committed: v.committed}] = true
				}
				measured := got.Objective.InputValue != nil
				if measured != (!v.stated && v.committed >= want.MeasurementSeq) || (measured && *got.Objective.InputValue != want.Input) {
					t.Fatalf("%s %+v: input %v, want %v from sequence %d", name, v, got.Objective.InputValue, want.Input, want.MeasurementSeq)
				}
				if got.Epoch != 1 {
					t.Fatalf("%s %+v: epoch %d", name, v, got.Epoch)
				}
			}
			if observer.scans > len(scanned) {
				t.Fatalf("%s seed %d: %d scans for %d previous-writer prefixes", name, seed, observer.scans, len(scanned))
			}
		}
	}
}

func syntheticLog(t *testing.T, dir string, records int, damagedAt int) {
	t.Helper()
	data := []byte(runlog.Magic)
	for seq := 1; seq <= records; seq++ {
		encoded, err := proto.Marshal(&storev1.Record{Seq: uint64(seq), Body: &storev1.Record_LegacyEvent{LegacyEvent: &storev1.LegacyEvent{Json: "{}"}}})
		if err != nil {
			t.Fatal(err)
		}
		data = binary.AppendUvarint(data, uint64(len(encoded)))
		data = binary.LittleEndian.AppendUint32(data, crc32.Checksum(encoded, crc32.MakeTable(crc32.Castagnoli)))
		data = append(data, encoded...)
	}
	if damagedAt > 0 {
		data[damagedAt] ^= 0xff
	}
	if err := os.WriteFile(filepath.Join(dir, "run.log"), data, 0600); err != nil {
		t.Fatal(err)
	}
}

func objectiveHead(t *testing.T, stated bool, committed uint64) *storev1.RunHead {
	t.Helper()
	// Any retained case supplies a valid head with an objective to build on.
	var template string
	for name := range corpusCases(t) {
		template = name
		break
	}
	stored, err := os.ReadFile(filepath.Join(corpus, template, "run.head"))
	if err != nil {
		t.Fatal(err)
	}
	head := &storev1.RunHead{}
	if err := proto.Unmarshal(stored, head); err != nil {
		t.Fatal(err)
	}
	head.CommittedSeq = proto.Uint64(committed)
	head.Objective.InputStated = stated
	head.PerformanceStated = true
	head.Objective.InputValue = nil
	return head
}

// Reading a head costs no scan of a long log when its writer stated the facts. A log that no
// scan could finish proves it: its damaged middle never matters to a current writer's head.
func TestCurrentWriterHeadOfALongLogCostsNoScan(t *testing.T) {
	const records = 100_000
	dir := t.TempDir()
	syntheticLog(t, dir, records, len(runlog.Magic)+records*10)
	var observer headObserver
	for _, hasValue := range []bool{false, true} {
		head := objectiveHead(t, true, records)
		if hasValue {
			head.Objective.InputValue = proto.Float64(100)
		}
		publish(t, dir, head)
		got, err := observer.read(dir)
		if err != nil || (got.Objective.InputValue != nil) != hasValue {
			t.Fatalf("read = %v, %v", got, err)
		}
	}
	if observer.scans != 0 {
		t.Fatalf("a current writer's head scanned the log %d times", observer.scans)
	}
	publish(t, dir, objectiveHead(t, false, records))
	if _, err := observer.read(dir); err == nil {
		t.Fatal("a previous writer's head did not read the damaged log")
	}
}

func TestPreviousWriterHeadOfALongLogScansOnce(t *testing.T) {
	const records = 100_000
	dir := t.TempDir()
	syntheticLog(t, dir, records, 0)
	publish(t, dir, objectiveHead(t, false, records))
	var observer headObserver
	for range 50 {
		got, err := observer.read(dir)
		if err != nil || got.Objective.InputValue != nil {
			t.Fatalf("read = %v, %v", got, err)
		}
	}
	if observer.scans != 1 {
		t.Fatalf("%d scans for one committed prefix", observer.scans)
	}
}
