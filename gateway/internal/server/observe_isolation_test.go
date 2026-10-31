package server_test

import (
	"context"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"testing"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/proto"
)

const retainedCorpus = "../../../proto/testdata/retained"

// corpusCases are the directories of the retained corpus: every previous writer's saved run.
func corpusCases(t *testing.T) []string {
	t.Helper()
	entries, err := os.ReadDir(retainedCorpus)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, entry := range entries {
		if entry.IsDir() {
			names = append(names, entry.Name())
		}
	}
	if len(names) == 0 {
		t.Fatal("the retained corpus has no case")
	}
	return names
}

func readCorpus(t *testing.T, name, file string) []byte {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(retainedCorpus, name, file))
	if err != nil {
		t.Fatal(err)
	}
	return data
}

// damages generates the corruptions of one run's data: cut logs, cut or garbled heads, a head
// whose commit fence reaches past its log, and garbage inside a committed prefix.
func damages(log, head []byte) []struct {
	name      string
	log, head []byte
} {
	type damage = struct {
		name      string
		log, head []byte
	}
	var out []damage
	for cut := 0; cut < len(log); cut += 4099 {
		out = append(out, damage{fmt.Sprintf("log cut at %d", cut), log[:cut], head})
	}
	for _, cut := range []int{len(log) - 1, 7, 8} {
		out = append(out, damage{fmt.Sprintf("log cut at %d", cut), log[:cut], head})
	}
	for cut := 0; cut < len(head); cut++ {
		out = append(out, damage{fmt.Sprintf("head cut at %d", cut), log, head[:cut]})
	}
	for seed := int64(0); seed < 16; seed++ {
		rng := rand.New(rand.NewSource(seed))
		garbled := append([]byte(nil), log...)
		at := rng.Intn(len(garbled))
		rng.Read(garbled[at:min(len(garbled), at+1+rng.Intn(64))])
		out = append(out, damage{fmt.Sprintf("log garbage %d at %d", seed, at), garbled, head})
		noise := make([]byte, 1+rng.Intn(len(head)))
		rng.Read(noise)
		out = append(out, damage{fmt.Sprintf("head garbage %d", seed), log, noise})
	}
	for _, fence := range []uint64{19, 20, 1 << 40} {
		fenced := &storev1.RunHead{}
		if err := proto.Unmarshal(head, fenced); err == nil {
			fenced.CommittedSeq = proto.Uint64(fence)
			data, _ := proto.Marshal(fenced)
			out = append(out, damage{fmt.Sprintf("fence %d past the log", fence), log, data})
		}
	}
	return out
}

// listing reads the initial listing of the host, which must complete.
func listing(t *testing.T, f fixture, c apiv1.HostServiceClient) map[string]*apiv1.RunEntry {
	t.Helper()
	ctx, cancel := context.WithCancel(f.ctx)
	defer cancel()
	stream, err := subscribeRuns(ctx, c)
	if err != nil {
		t.Fatal(err)
	}
	runs := map[string]*apiv1.RunEntry{}
	for {
		got, err := stream.Recv()
		if err != nil {
			t.Fatalf("listing ended before its initial marker: %v", err)
		}
		switch {
		case got.InitialComplete:
			return runs
		case got.RunId != "":
			runs[got.RunId] = got.RunEntry
		}
	}
}

// A read error in one run's data stays inside that run: the other runs list with the same
// facts, and the damaged run is still listed by name.
func TestRunsListsHealthyRunsBesideADamagedOne(t *testing.T) {
	for _, name := range corpusCases(t) {
		log, head := readCorpus(t, name, "run.log"), readCorpus(t, name, "run.head")
		f := start(t)
		second := t.TempDir()
		if err := os.Symlink(second, filepath.Join(f.root, "runs", "run-2")); err != nil {
			t.Fatal(err)
		}
		write(t, filepath.Join(second, "run.log"), false, log)
		write(t, filepath.Join(second, "run.head"), false, head)
		c := client(t, f, false)
		write(t, filepath.Join(f.run, "run.log"), false, log)
		write(t, filepath.Join(f.run, "run.head"), false, head)
		baseline := listing(t, f, c)
		if baseline["run-2"].GetHead().GetObjective() == nil {
			t.Fatalf("%s: the healthy run lists without its head: %v", name, baseline["run-2"])
		}
		for _, damage := range damages(log, head) {
			write(t, filepath.Join(f.run, "run.log"), false, damage.log)
			write(t, filepath.Join(f.run, "run.head"), false, damage.head)
			runs := listing(t, f, c)
			if !proto.Equal(runs["run-2"], baseline["run-2"]) {
				t.Fatalf("%s, %s: the healthy run changed: %v, want %v", name, damage.name, runs["run-2"], baseline["run-2"])
			}
			if runs["run-1"] == nil {
				t.Fatalf("%s, %s: the damaged run is not listed by name", name, damage.name)
			}
		}
	}
}
