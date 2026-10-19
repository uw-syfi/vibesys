package devlaunch

import (
	"encoding/json"
	"fmt"
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
