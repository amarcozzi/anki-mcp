// Command anki-mcp serves Anki reviews over MCP, backed by AnkiWeb.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/amarcozzi/anki-mcp/internal/ankiweb"
	"github.com/amarcozzi/anki-mcp/internal/auth"
	"github.com/amarcozzi/anki-mcp/internal/config"
	"github.com/amarcozzi/anki-mcp/internal/tools"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	cfg, err := config.Load()
	if err != nil {
		log.Error("config", "err", err)
		os.Exit(2)
	}

	anki := ankiweb.New(cfg.AnkiWebUsername, cfg.AnkiWebPassword)
	oauth := auth.New(cfg.BaseURL, cfg.OwnerPassword, []byte(cfg.JWTSigningKey), cfg.ExtraRedirectURIs, log)

	server := mcp.NewServer(&mcp.Implementation{Name: "anki-mcp", Version: "0.1.0"}, &mcp.ServerOptions{
		Instructions: "You are helping the user review Anki flashcards by conversation. " +
			"Call next_card, show the FRONT and wait for the user's answer. Then call reveal, compare, and propose a rating " +
			"(1 Again, 2 Hard, 3 Good, 4 Easy). Grade with answer once the user confirms, or immediately if they asked you to auto-grade. " +
			"Keep it brisk: one card per exchange. Never invent card content.",
	})
	tools.New(anki).Register(server)

	mcpHandler := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server },
		&mcp.StreamableHTTPOptions{Stateless: true, Logger: log})

	mux := http.NewServeMux()
	oauth.Register(mux)
	mux.Handle("/mcp", oauth.RequireBearer(mcpHandler))
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) { w.Write([]byte("ok")) })
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte("anki-mcp: connect an MCP client to /mcp\n"))
	})

	srv := &http.Server{Addr: ":" + cfg.Port, Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		log.Info("listening", "port", cfg.Port, "base_url", cfg.BaseURL)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("serve", "err", err)
			os.Exit(1)
		}
	}()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 8*time.Second) // Cloud Run allows 10s
	defer cancel()
	srv.Shutdown(shutdownCtx)
}
