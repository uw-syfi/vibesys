package server

import (
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
