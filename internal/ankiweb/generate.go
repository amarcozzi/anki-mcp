// Package ankiweb is a client for the private HTTP API behind AnkiWeb's study page.
//
// The protocol is undocumented and unsupported. It was reverse-engineered from the
// study page's JS bundle; see docs/investigation.md and ankiweb.proto.
package ankiweb

//go:generate protoc --go_out=ankiwebpb --go_opt=paths=source_relative --go_opt=Mankiweb.proto=github.com/amarcozzi/anki-mcp/internal/ankiweb/ankiwebpb ankiweb.proto
