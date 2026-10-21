package server

import "github.com/fsnotify/fsnotify"

func newRunWatcher() (*fsnotify.Watcher, error) { return fsnotify.NewWatcher() }
