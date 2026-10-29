package devlaunch

import (
	"encoding/json"
	"fmt"
	"strings"
)

// hostsFile is the desktop app's hosts.json (version 2), whose one writer is the app; this command
// reads the checkout the profile saved for a machine and seeds a profile that has no file yet.
type hostsFile struct {
	Version int                    `json:"version"`
	Hosts   map[string]hostSetting `json:"hosts"`
}

type hostSetting struct {
	Checkout string `json:"checkout"`
}

func hostKey(alias string) string { return "ssh:" + alias }

// SavedCheckout is the checkout hosts.json saves for the machine, or "" when there is none or the
// file is not a version 2 hosts.json (the app reports that file's problems itself).
func SavedCheckout(raw []byte, alias string) string {
	var f hostsFile
	if json.Unmarshal(raw, &f) != nil || f.Version != 2 {
		return ""
	}
	return f.Hosts[hostKey(alias)].Checkout
}

// SeedHosts is a hosts.json that names the machine's checkout, so a fresh profile opens with the
// machine set up.
func SeedHosts(alias, checkout string) ([]byte, error) {
	out, err := json.MarshalIndent(hostsFile{Version: 2, Hosts: map[string]hostSetting{hostKey(alias): {checkout}}}, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("seed hosts.json: %w", err)
	}
	return append(out, '\n'), nil
}

// HostsFile is one place the app's saved hosts may be: its path and, when the file exists, its bytes.
type HostsFile struct {
	Path  string
	Data  []byte
	Found bool
}

// ResolveCheckout is the machine's checkout: the one the user named, else the one the app saved
// for it in the first hosts file that exists. The error names every file it looked at.
func ResolveCheckout(named, alias string, files []HostsFile) (string, error) {
	if named != "" {
		return named, nil
	}
	var looked []string
	for _, f := range files {
		if !f.Found {
			looked = append(looked, f.Path+" (missing)")
			continue
		}
		if c := SavedCheckout(f.Data, alias); c != "" {
			return c, nil
		}
		looked = append(looked, f.Path+" (no entry for the machine)")
	}
	return "", fmt.Errorf("the checkout of machine %q is not saved: looked in %s; add the machine in the app's Machines screen or pass --checkout PATH", alias, strings.Join(looked, ", "))
}
