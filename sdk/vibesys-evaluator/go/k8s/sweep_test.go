package k8s_test

import (
	"context"
	"fmt"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s"
)

// ttl is the task's longest timeout_seconds plus the margin.
const ttl = 3600*time.Second + 600*time.Second

// The sweep deletes exactly this task's stale namespaces, never another's.
func TestOnlyThisTasksOldNamespacesAreDeleted(t *testing.T) {
	w := newWorld(t)
	now := w.clock.Now()
	tasks := []string{"shop", "shop-2", "other", ""}
	prefixes := []string{"hk8s-shop-", "hk8s-shop-2-", "hk8s-other-", "kube-"}
	ages := []time.Duration{0, ttl - time.Second, ttl + time.Second, 10 * ttl}
	expected := map[string]bool{}
	var all []string
	index := 0
	for _, task := range tasks {
		for _, prefix := range prefixes {
			for _, age := range ages {
				for _, withLabel := range []bool{false, true} {
					name := fmt.Sprintf("%s%04x", prefix, index)
					index++
					labels := map[string]string{}
					if task != "" {
						labels[k8s.TaskLabel] = task
					}
					if withLabel {
						labels[k8s.CreatedLabel] = strconv.FormatInt(now.Add(-age).Unix(), 10)
					}
					w.kube.AddNamespace(name, labels, now.Add(-age))
					all = append(all, name)
					if task == "shop" && strings.HasPrefix(name, "hk8s-shop-") && !strings.Contains(strings.TrimPrefix(name, "hk8s-shop-"), "-") && age > ttl {
						expected[name] = true
					}
				}
			}
		}
	}
	swept, err := k8s.Sweep(context.Background(), w.source(makeApp()), w.cluster())
	if err != nil {
		t.Fatal(err)
	}
	if len(swept) == 0 || len(swept) != len(expected) {
		t.Fatalf("swept %d, want %d", len(swept), len(expected))
	}
	for _, name := range swept {
		if !expected[name] {
			t.Fatalf("swept %s", name)
		}
	}
	var survivors []string
	for _, name := range all {
		if !expected[name] {
			survivors = append(survivors, name)
		}
	}
	slices.Sort(survivors)
	if got := w.kube.LiveNamespaces(); !slices.Equal(got, survivors) {
		t.Fatalf("survivors %v, want %v", got, survivors)
	}
}

func TestANamespaceRecreatedBetweenListingAndDeletionIsKept(t *testing.T) {
	w := newWorld(t)
	old := w.clock.Now().Add(-10 * ttl)
	w.kube.AddNamespace("hk8s-shop-aaaa", map[string]string{k8s.TaskLabel: "shop", k8s.CreatedLabel: strconv.FormatInt(old.Unix(), 10)}, old)
	// Call 1 lists; call 2 is the deletion: another job recreates the namespace just before.
	w.kube.AtCall(2, func() { w.kube.Recreate("hk8s-shop-aaaa") })
	swept, err := k8s.Sweep(context.Background(), w.source(makeApp()), w.cluster())
	if err != nil || len(swept) != 0 {
		t.Fatalf("%v %v", swept, err)
	}
	if got := w.kube.LiveNamespaces(); !slices.Equal(got, []string{"hk8s-shop-aaaa"}) {
		t.Fatalf("%v", got)
	}
	found := false
	for _, line := range w.lines {
		found = found || strings.Contains(line, "Conflict")
	}
	if !found {
		t.Fatalf("no conflict logged: %v", w.lines)
	}
}
