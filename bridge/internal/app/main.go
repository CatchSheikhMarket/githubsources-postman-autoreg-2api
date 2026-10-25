// main.go — HTTP-сервер, конфиг (.env), роуты, дашборд.
// Апстрим: postman.go (gateway.postman.com). Ручки: chat.go (OpenAI), anthropic.go (Anthropic).
package app

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"time"
)

// ── Config ──

type Config struct {
	Host         string
	Port         string
	AuthToken    string // токен, который мост спрашивает у клиентов
	BaseURL      string
	Token        string // запасной access_token Postman
	SidCookie    string // запасной кука postman.sid веб-сессии (POSTMAN_SID_COOKIE)
	SessionsFile string
	DefaultModel string
	Timeout      int // сек на один upstream-запрос
	AppVersion   string
	Platform     string
	Product      string // product без клиентских инструментов
	ProductTools string // product с клиентскими инструментами (thirdParty)
	WorkspaceID  string
	ToolsHash    string
	TermsHash    string
	ToolsHashSDK string // хэши clientTools для product tools
	TermsHashSDK string
	DataDir      string // каталог данных Postman Desktop
	MaxQuery     int    // байт в input.query (у Postman жёсткий лимит ~10k)
	FromDesktop  bool   // токен подхвачен из установленного Postman Desktop
}

// Хэши clientTools/clientKBTerms выдаются сервером Postman и привязаны к версии
// приложения. Дефолты сняты с Postman 12.23.1 (значения из рабочих мостов).
// Если апстрим ответит TOOL_VALIDATION_ERROR — перехвати свежие значения
// (sniff gateway.postman.com/chat) и задай через POSTMAN_TOOLS_HASH /
// POSTMAN_TERMS_HASH в .env.
const (
	defaultToolsHash = "clienttools-workspace_localmode_v12-desktop-darwin-12.23.1-ui-260811-0231-828d6b3ed37b"
	defaultTermsHash = "kbterms-workspace_localmode_v12-desktop-darwin-12.23.1-ui-260811-0231-dbc2c7575e92"

	// Клиентские инструменты Postman обслуживает в дочернем product'е
	// workspace_tools_sdk_localmode — со своими хэшами clientTools/clientKBTerms.
	defaultProductTools = "workspace_tools_sdk_localmode"
	defaultToolsHashSDK = "clienttools-workspace_tools_sdk_localmode-desktop-darwin-12.23.1-ui-260811-0231-e23d250ee33a"
	defaultTermsHashSDK = "kbterms-workspace_tools_sdk_localmode-desktop-darwin-12.23.1-ui-260811-0231-dbc2c7575e92"
)

func loadConfig() *Config {
	return &Config{
		Host:         envOr("HOST", "127.0.0.1"),
		Port:         envOr("PORT", "12049"),
		AuthToken:    envOr("AUTH_TOKEN", "postman2api"),
		BaseURL:      strings.TrimRight(envOr("POSTMAN_API_BASE", "https://gateway.postman.com"), "/"),
		Token:        envOr("POSTMAN_ACCESS_TOKEN", ""),
		SidCookie:    envOr("POSTMAN_SID_COOKIE", ""),
		SessionsFile: envOr("SESSIONS_FILE", "sessions.txt"),
		DefaultModel: envOr("DEFAULT_MODEL", "gpt-6.1-sol"),
		Timeout:      envIntOr("TIMEOUT", 300),
		AppVersion:   envOr("POSTMAN_APP_VERSION", "12.23.1"),
		Platform:     envOr("POSTMAN_PLATFORM", "DESKTOP_LINUX"),
		Product:      envOr("POSTMAN_PRODUCT", "workspace_localmode_v12"),
		ProductTools: envOr("POSTMAN_PRODUCT_TOOLS", defaultProductTools),
		WorkspaceID:  envOr("POSTMAN_WORKSPACE_ID", "proxy-workspace"),
		ToolsHash:    envOr("POSTMAN_TOOLS_HASH", defaultToolsHash),
		TermsHash:    envOr("POSTMAN_TERMS_HASH", defaultTermsHash),
		ToolsHashSDK: envOr("POSTMAN_TOOLS_HASH_SDK", defaultToolsHashSDK),
		TermsHashSDK: envOr("POSTMAN_TERMS_HASH_SDK", defaultTermsHashSDK),
		DataDir:      defaultDataDir(),
		MaxQuery:     envIntOr("MAX_QUERY_BYTES", 9800),
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envIntOr(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		var n int
		if _, err := fmt.Sscanf(v, "%d", &n); err == nil && n > 0 {
			return n
		}
	}
	return def
}

func loadDotEnv(path string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		k = strings.TrimSpace(k)
		v = strings.Trim(strings.TrimSpace(v), `"'`)
		if k != "" && os.Getenv(k) == "" {
			os.Setenv(k, v)
		}
	}
}

// ── Server ──

type Server struct {
	cfg       *Config
	up        *Upstream
	tools     *toolRegistry // toolCallId → conversationId (для TOOL_RESPONSE)
	startedAt time.Time
	stats     Stats
}

func sessionFileEmpty(path string) bool {
	data, err := os.ReadFile(path)
	if err != nil {
		return true
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line != "" && !strings.HasPrefix(line, "#") {
			return false
		}
	}
	return true
}

func Main(args []string) {
	loadDotEnv(".env")

	cfg := loadConfig()

	// Токена в .env нет — пробуем добавить сессию установленного Postman
	// Desktop к аккаунтам из sessions.txt (если Desktop не залогинен, а
	// сессий больше нет нигде — это фатально).
	if cfg.Token == "" {
		token, appVersion, err := importDesktopSession(cfg.DataDir)
		switch {
		case err == nil:
			cfg.Token = token
			cfg.FromDesktop = true
			if os.Getenv("POSTMAN_APP_VERSION") == "" && appVersion != "" {
				cfg.AppVersion = appVersion
			}
		case sessionFileEmpty(cfg.SessionsFile) && cfg.SidCookie == "":
			log.Fatalf("[BOOT] нет токена Postman: %v\n"+
				"  Что сделать: (1) залогинься в Postman Desktop на этой машине — мост подхватит сессию сам;\n"+
				"                (2) впиши access_token в POSTMAN_ACCESS_TOKEN (.env);\n"+
				"                (3) положи токены аккаунтов в %s (по одному на строку)", err, cfg.SessionsFile)
		}
	}

	s := &Server{
		cfg:       cfg,
		up:        NewUpstream(cfg),
		tools:     newToolRegistry(),
		startedAt: time.Now(),
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/", s.handleRoot)
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/status", s.handleStatus)
	mux.HandleFunc("/v1/models", s.handleModels) // без auth — для GUI и дашборда
	mux.HandleFunc("/v1/chat/completions", s.auth(s.handleChatCompletions))
	mux.HandleFunc("/v1/messages", s.auth(s.handleMessages))

	addr := cfg.Host + ":" + cfg.Port
	log.Printf("[BOOT] postman2api слушает http://%s", addr)
	log.Printf("[BOOT] upstream: %s (platform=%s, app-version=%s)", cfg.BaseURL, cfg.Platform, cfg.AppVersion)
	log.Printf("[BOOT] AUTH_TOKEN: %s", cfg.AuthToken)
	log.Printf("[BOOT] модель по умолчанию: %s", cfg.DefaultModel)
	if cfg.FromDesktop {
		log.Printf("[BOOT] сессия: Postman Desktop (%s)", cfg.DataDir)
	}
	if alive, total := s.up.sessions.Stats(); total > 0 {
		log.Printf("[BOOT] аккаунтов: %d/%d живы", alive, total)
	}
	log.Printf("[BOOT] Ctrl+C — остановить")

	// WriteTimeout не ставим: SSE-стримы долгие, резать их нельзя
	srv := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 30 * time.Second, ReadTimeout: 120 * time.Second, IdleTimeout: 120 * time.Second}
	if err := srv.ListenAndServe(); err != nil {
		log.Fatalf("[BOOT] %v", err)
	}
}

// ── Auth ──

func (s *Server) auth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		token := ""
		if h := r.Header.Get("Authorization"); strings.HasPrefix(h, "Bearer ") {
			token = strings.TrimPrefix(h, "Bearer ")
		}
		if token == "" {
			token = r.Header.Get("x-api-key")
		}
		if s.cfg.AuthToken != "" && token != s.cfg.AuthToken {
			writeError(w, http.StatusUnauthorized, "invalid auth token (нужен Bearer или x-api-key)")
			return
		}
		next(w, r)
	}
}

// ── Status / health / dashboard ──

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok"))
}

func (s *Server) sessionSource() string {
	switch {
	case s.cfg.FromDesktop:
		return "postman-desktop"
	case len(s.cfg.Token) > 0:
		return "env"
	default:
		return "sessions.txt"
	}
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	alive, total := s.up.sessions.Stats()
	writeJSON(w, http.StatusOK, map[string]any{
		"uptime":         time.Since(s.startedAt).Round(time.Second).String(),
		"requests":       s.stats.Requests(),
		"errors":         s.stats.Errors(),
		"upstream":       s.cfg.BaseURL,
		"platform":       s.cfg.Platform,
		"app_version":    s.cfg.AppVersion,
		"default_model":  s.cfg.DefaultModel,
		"session_source": s.sessionSource(),
		"sessions_alive": alive,
		"sessions_total": total,
	})
}

//go:embed dashboard.html
var dashboardHTML string

func (s *Server) handleRoot(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write([]byte(dashboardHTML))
}

// ── Helpers ──

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{
		"error": map[string]any{
			"message": msg,
			"type":    "bridge_error",
			"code":    status,
		},
	})
}
