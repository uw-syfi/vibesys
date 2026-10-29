package server

import (
	"bytes"
	"encoding/binary"
	"errors"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"hash/crc32"
	"io"
	"os"
	"path/filepath"
	"testing"

	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/proto"
)

// test-isolation: the observation scan counter measures the retained-prefix work bound.
func TestDamagedPrefixesScanOnceAndIdentityChangesRetry(t *testing.T) {
	for name := range corpusCases(t) {
		original, err := os.ReadFile(filepath.Join(corpus, name, "run.log"))
		if err != nil {
			t.Fatal(err)
		}
		stored, err := os.ReadFile(filepath.Join(corpus, name, "run.head"))
		if err != nil {
			t.Fatal(err)
		}
		for variant := 0; variant < 12; variant++ {
			dir := t.TempDir()
			head := &storev1.RunHead{}
			if err := proto.Unmarshal(stored, head); err != nil {
				t.Fatal(err)
			}
			damaged := append([]byte(nil), original...)
			switch variant % 3 {
			case 0:
				damaged = damaged[:len(damaged)-1-variant*len(damaged)/12]
			case 1:
				damaged[9] ^= 255
			case 2:
				head.CommittedSeq = proto.Uint64(head.GetCommittedSeq() + uint64(variant) + 1)
			}
			log := filepath.Join(dir, "run.log")
			if err := os.WriteFile(log, damaged, 0600); err != nil {
				t.Fatal(err)
			}
			head.Objective.InputStated = false
			head.Objective.InputValue = nil
			publish(t, dir, head)
			var observer headObserver
			for range 10 {
				if _, err := observer.read(dir); err == nil {
					t.Fatalf("%s variant %d: expected damage", name, variant)
				}
			}
			if observer.scans != 1 {
				t.Fatalf("%s variant %d: same damaged prefix scanned %d times, want 1", name, variant, observer.scans)
			}
			if err := os.WriteFile(log, original, 0600); err != nil {
				t.Fatal(err)
			}
			if err := proto.Unmarshal(stored, head); err != nil {
				t.Fatal(err)
			}
			head.Objective.InputStated = false
			head.Objective.InputValue = nil
			publish(t, dir, head)
			if _, err := observer.read(dir); err != nil {
				t.Fatal(err)
			}
			if observer.scans != 2 {
				t.Fatalf("repaired prefix not retried: %d scans", observer.scans)
			}
		}
	}
}

// test-isolation: a closed descriptor injects a real read failure after the prefix identity was
// acquired. The observation's public scan bound must exclude transient I/O outcomes.
func TestTransientPrefixReadFailureIsRetried(t *testing.T) {
	dir := t.TempDir()
	syntheticLog(t, dir, 1, 0)
	path := filepath.Join(dir, "run.log")
	file, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	info, err := file.Stat()
	if err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	head := objectiveHead(t, false, 1)
	var observer headObserver
	for range 2 {
		if _, err := observer.recovered(file, info, head); err == nil {
			t.Fatal("closed descriptor read succeeded")
		}
	}
	reopened, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if _, err := observer.recovered(reopened, info, head); err != nil {
		t.Fatalf("transient failure was memoized: %v", err)
	}
	if observer.scans != 3 {
		t.Fatalf("transient reads did not retry: %d scans", observer.scans)
	}
}

// test-isolation: scans measures the committed-prefix work bound at the observation boundary.
func TestSemanticProjectionDamageIsMemoizedAndRepairRetries(t *testing.T) {
	original, err := os.ReadFile(filepath.Join(corpus, "64c1a8b7", "run.log"))
	if err != nil {
		t.Fatal(err)
	}
	stored, err := os.ReadFile(filepath.Join(corpus, "64c1a8b7", "run.head"))
	if err != nil {
		t.Fatal(err)
	}
	for _, kind := range []string{"gate", "workstream"} {
		t.Run(kind, func(t *testing.T) {
			reader := runlog.NewReader(bytes.NewReader(original))
			damaged := []byte(runlog.Magic)
			changed := false
			for {
				payload, err := reader.Next()
				if errors.Is(err, io.EOF) {
					break
				}
				if err != nil {
					t.Fatal(err)
				}
				record := &storev1.Record{}
				if err := proto.Unmarshal(payload, record); err != nil {
					t.Fatal(err)
				}
				if label := record.GetSpan().GetSpan().GetLabel(); label.GetKind() == kind && label.Attrs != nil {
					label.Attrs.Value = []byte{255}
					changed = true
				}
				data, err := proto.Marshal(record)
				if err != nil {
					t.Fatal(err)
				}
				damaged = binary.AppendUvarint(damaged, uint64(len(data)))
				damaged = binary.LittleEndian.AppendUint32(damaged, crc32.Checksum(data, crc32.MakeTable(crc32.Castagnoli)))
				damaged = append(damaged, data...)
			}
			if !changed {
				t.Fatal("fixture lacks requested attrs")
			}
			dir := t.TempDir()
			log := filepath.Join(dir, "run.log")
			if err := os.WriteFile(log, damaged, 0600); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(dir, "run.head"), stored, 0600); err != nil {
				t.Fatal(err)
			}
			var observer headObserver
			for range 10 {
				if _, err := observer.read(dir); err == nil {
					t.Fatal("invalid retained attrs were accepted")
				}
			}
			if observer.scans != 1 {
				t.Fatalf("semantic damage scanned %d times", observer.scans)
			}
			if err := os.WriteFile(log, original, 0600); err != nil {
				t.Fatal(err)
			}
			if head, err := observer.read(dir); err != nil || !head.GetPerformanceStated() {
				t.Fatalf("repair: %v, %v", head, err)
			}
			if observer.scans != 2 {
				t.Fatalf("repair scans = %d", observer.scans)
			}
		})
	}
}

// test-isolation: the scan bound spans the public head and project observations.
func TestUnfencedRetainedHeadAndProjectShareOnePrefix(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{"run.head", "run.log"} {
		data, err := os.ReadFile(filepath.Join(corpus, "438bfd3d", name))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, name), data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	raw, err := os.ReadFile(filepath.Join(dir, "run.head"))
	if err != nil {
		t.Fatal(err)
	}
	head := &storev1.RunHead{}
	if err := proto.Unmarshal(raw, head); err != nil {
		t.Fatal(err)
	}
	head.CommittedSeq = nil
	publish(t, dir, head)
	var observer headObserver
	observed, err := observer.read(dir)
	if err != nil {
		t.Fatal(err)
	}
	root, err := observer.project(t.TempDir(), "run-a", dir, observed)
	if root != "" || !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("an outside-layout run used a saved project: %q, %v", root, err)
	}

	if observer.scans != 1 {
		t.Fatalf("same frozen prefix scanned %d times", observer.scans)
	}
}
