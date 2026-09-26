package main

import (
	"repoctl/discovery"
	"repoctl/discovery/native"
	"repoctl/discovery/python"
	"repoctl/discovery/typescript"
	"repoctl/internal/command"
)

var discoveryAdapters = map[string]discovery.Adapter{
	"tach":                 python.Adapter{},
	"package_json":         typescript.Adapter{},
	"manifest_directories": native.Adapter{Run: command.Run},
}
