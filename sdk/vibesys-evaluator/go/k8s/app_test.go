package k8s_test

import (
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s"
)

func TestAWrongFieldIsNamed(t *testing.T) {
	for _, c := range []struct {
		name   string
		mutate func(*k8s.App)
		names  string
	}{
		{"name", func(a *k8s.App) { a.Name = "Shop" }, "name="},
		{"abs", func(a *k8s.App) { a.Manifest = "/abs/manifest.yaml" }, "manifest="},
		{"dotdot", func(a *k8s.App) { a.Manifest = "../manifest.yaml" }, "manifest="},
		{"lower", func(a *k8s.App) { a.Images = map[string]string{"app": "app"} }, "images"},
		{"namespace", func(a *k8s.App) { a.Images = map[string]string{"NAMESPACE": "app"} }, "images"},
		{"imagepath", func(a *k8s.App) { a.Images = map[string]string{"APP_IMAGE": "../app"} }, `images['APP_IMAGE']`},
		{"https", func(a *k8s.App) { a.Gateway = "https://frontend:5000" }, "gateway="},
		{"path", func(a *k8s.App) { a.Gateway = "http://frontend:5000/api" }, "gateway="},
		{"host", func(a *k8s.App) { a.Gateway = "http://Front_End:5000" }, "gateway="},
		{"tier", func(a *k8s.App) { a.DataTier = "tier in (data)" }, "data_tier="},
		{"noseed", func(a *k8s.App) { a.HasSeed = false }, "data_tier="},
		{"repeat", func(a *k8s.App) { a.Restartable = []string{"frontend", "frontend"} }, "restartable"},
		{"cache", func(a *k8s.App) { a.Caches = map[string]k8s.CacheKind{"cache": "varnish"} }, `caches['cache']`},
	} {
		t.Run(c.name, func(t *testing.T) {
			err := makeApp(c.mutate).Validate()
			if err == nil || !strings.Contains(err.Error(), c.names) {
				t.Fatalf("want an error naming %q, got %v", c.names, err)
			}
		})
	}
	if err := makeApp().Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestNamesAreDNSSafeOrRefused(t *testing.T) {
	for name, valid := range map[string]bool{
		"shop": true, "a": true, "a-b": true, "shop2": true, "2shop": false, "shop-": false, "-shop": false,
		"Shop": false, "sh_op": false, strings.Repeat("a", 40): true, strings.Repeat("a", 41): false, "": false,
	} {
		err := makeApp(func(a *k8s.App) { a.Name = name }).Validate()
		if (err == nil) != valid {
			t.Fatalf("%q: valid=%v err=%v", name, valid, err)
		}
	}
}

func TestParseGateway(t *testing.T) {
	g, err := k8s.ParseGateway("http://Frontend:5000/")
	if err != nil || g.Service != "frontend" || g.Port != 5000 || g.URL != "http://Frontend:5000" {
		t.Fatalf("%+v %v", g, err)
	}
	if g, err = k8s.ParseGateway("http://web"); err != nil || g.Port != 80 {
		t.Fatalf("%+v %v", g, err)
	}
}

func TestParseSelector(t *testing.T) {
	got, err := k8s.ParseSelector("tier=data, app.kubernetes.io/name==db")
	if err != nil || got["tier"] != "data" || got["app.kubernetes.io/name"] != "db" {
		t.Fatalf("%v %v", got, err)
	}
	for _, bad := range []string{"tier", "tier!=data", "=x", "tier in (a)"} {
		if _, err := k8s.ParseSelector(bad); err == nil {
			t.Fatalf("%q accepted", bad)
		}
	}
}
