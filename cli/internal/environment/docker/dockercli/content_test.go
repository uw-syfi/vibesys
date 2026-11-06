package dockercli_test

import (
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockercli"
)

// inspected is an image as `docker image inspect` writes it, in the parts the tests vary.
type inspected struct {
	id, created string
	tags        []string
	env, cmd    []string
	labels      map[string]string
	os, arch    string
	layers      []string
}

func randomImage(r *rand.Rand) inspected {
	hexOf := func() string {
		return fmt.Sprintf("%016x%016x%016x%016x", r.Uint64(), r.Uint64(), r.Uint64(), r.Uint64())
	}
	img := inspected{
		id: "sha256:" + hexOf(), created: fmt.Sprintf("2026-11-0%dT10:00:00Z", 1+r.IntN(9)),
		tags: []string{fmt.Sprintf("vibesys-candidate:agentx-%x", r.Uint64())},
		env:  []string{"PATH=/usr/bin", fmt.Sprintf("V=%d", r.IntN(1000))}, cmd: []string{"python", "-m", "engine"},
		labels: map[string]string{"a": fmt.Sprint(r.IntN(10)), "b": "x"},
		os:     "linux", arch: []string{"amd64", "arm64"}[r.IntN(2)],
	}
	for range 1 + r.IntN(5) {
		img.layers = append(img.layers, "sha256:"+hexOf())
	}
	return img
}

// json writes the image as Docker does, with the keys of each object in the order r draws.
func (img inspected) json(r *rand.Rand) []byte {
	q := func(v any) string { b, _ := json.Marshal(v); return string(b) }
	object := func(fields map[string]string) string {
		keys := make([]string, 0, len(fields))
		for k := range fields {
			keys = append(keys, k)
		}
		slices.Sort(keys)
		r.Shuffle(len(keys), func(i, j int) { keys[i], keys[j] = keys[j], keys[i] })
		var parts []string
		for _, k := range keys {
			parts = append(parts, q(k)+":"+fields[k])
		}
		return "{" + strings.Join(parts, ",") + "}"
	}
	labels := map[string]string{}
	for k, v := range img.labels {
		labels[k] = q(v)
	}
	config := object(map[string]string{
		"Env": q(img.env), "Cmd": q(img.cmd), "Entrypoint": "null", "WorkingDir": q("/app"), "User": q(""),
		"Labels": object(labels),
	})
	return []byte(object(map[string]string{
		"Id": q(img.id), "RepoTags": q(img.tags), "RepoDigests": q([]string{"vibesys-candidate@" + img.id}),
		"Created": q(img.created), "Config": config, "Os": q(img.os), "Architecture": q(img.arch),
		"RootFS":     object(map[string]string{"Type": q("layers"), "Layers": q(img.layers)}),
		"Metadata":   object(map[string]string{"LastTagTime": q(img.created)}),
		"Descriptor": object(map[string]string{"digest": q(img.id), "size": "856"}),
	}))
}

func digest(t *testing.T, inspect []byte) string {
	t.Helper()
	d, err := dockercli.ContentDigest(inspect)
	if err != nil {
		t.Fatalf("%s: %v", inspect, err)
	}
	if h, ok := strings.CutPrefix(d, "sha256:"); !ok || len(h) != 64 || strings.Trim(h, "0123456789abcdef") != "" {
		t.Fatalf("the digest %q is not sha256: and 64 hexadecimal digits", d)
	}
	return d
}

// Seeded random images: what names one build of an image (its ID, tags, creation, descriptor) and
// the order of the keys Docker writes never change the digest of its content; a change to its
// layers (one changed, one more, or their order), its config or its platform always does.
func TestTheContentDigestIsOfWhatAContainerRunsOnly(t *testing.T) {
	for seed := range uint64(200) {
		r := rand.New(rand.NewPCG(seed, 11))
		img := randomImage(r)
		want := digest(t, img.json(r))

		rebuilt := img
		other := randomImage(r)
		rebuilt.id, rebuilt.tags, rebuilt.created = other.id, other.tags, other.created
		if got := digest(t, rebuilt.json(r)); got != want {
			t.Errorf("seed %d: another build of one content has the digest %s, want %s", seed, got, want)
		}

		changes := map[string]func(*inspected){
			"a layer":         func(i *inspected) { i.layers[r.IntN(len(i.layers))] = other.layers[0] },
			"one more layer":  func(i *inspected) { i.layers = append(i.layers, other.layers[0]) },
			"the layer order": func(i *inspected) { i.layers = append(i.layers, i.layers[0])[1:] },
			"the environment": func(i *inspected) { i.env = append(i.env, "W=1") },
			"the command":     func(i *inspected) { i.cmd = []string{"sh"} },
			"a label":         func(i *inspected) { i.labels["c"] = "1" },
			"the platform":    func(i *inspected) { i.arch = map[string]string{"amd64": "arm64", "arm64": "amd64"}[i.arch] },
		}
		for what, change := range changes {
			changed := img
			changed.layers, changed.env = slices.Clone(img.layers), slices.Clone(img.env)
			changed.labels = map[string]string{}
			for k, v := range img.labels {
				changed.labels[k] = v
			}
			change(&changed)
			if what == "the layer order" && len(changed.layers) == 1 {
				continue
			}
			if got := digest(t, changed.json(r)); got == want {
				t.Errorf("seed %d: changing %s left the digest %s", seed, what, got)
			}
		}
	}
}

// What is not an image, or one with no layers or platform, has no digest.
func TestWhatIsNotAnImageHasNoContentDigest(t *testing.T) {
	for _, inspect := range []string{"", "[]", "{}", `{"Os":"linux","Architecture":"amd64"}`, `{"RootFS":{"Layers":[]},"Os":"linux"}`, `{"RootFS":{},"Os":"linux","Architecture":"amd64","Config":{`} {
		if d, err := dockercli.ContentDigest([]byte(inspect)); err == nil {
			t.Errorf("%q has the digest %s", inspect, d)
		}
	}
}
