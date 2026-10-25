package app

import (
	"bufio"
	"os"
	"strings"
	"sync"
)

// Session — одна учётка Postman. kind="token" — access_token (шлётся в
// x-access-token), kind="cookie" — кука postman.sid веб-сессии (шлётся в
// Cookie; gateway принимает её как авторизацию веб-версии).
type Session struct {
	Value  string
	Cookie bool
}

// SessionPool ротирует сессии Postman (по одной на аккаунт) и исключает из
// ротации те, что апстрим отбил 401, до перезапуска моста.
type SessionPool struct {
	mu      sync.Mutex
	sess    []Session
	invalid map[string]bool
	next    int
}

func NewSessionPool(path, fallbackToken, fallbackCookie string) *SessionPool {
	p := &SessionPool{invalid: make(map[string]bool)}
	if f, err := os.Open(path); err == nil {
		s := bufio.NewScanner(f)
		for s.Scan() {
			p.add(parseSession(s.Text()))
		}
		_ = f.Close()
	}
	p.add(Session{Value: strings.TrimSpace(fallbackToken)})
	p.add(Session{Value: strings.TrimSpace(fallbackCookie), Cookie: true})
	return p
}

// parseSession разбирает строку sessions.txt: "cookie:<postman.sid>" —
// веб-сессия, остальное — access_token.
func parseSession(line string) Session {
	line = strings.TrimSpace(line)
	if len(line) > 7 && strings.EqualFold(line[:7], "cookie:") {
		return Session{Value: strings.TrimSpace(line[7:]), Cookie: true}
	}
	return Session{Value: line}
}

func (p *SessionPool) add(s Session) {
	if s.Value == "" || strings.HasPrefix(s.Value, "#") {
		return
	}
	for _, existing := range p.sess {
		if existing == s {
			return
		}
	}
	p.sess = append(p.sess, s)
}

func (p *SessionPool) Next() (Session, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for range len(p.sess) {
		s := p.sess[p.next%len(p.sess)]
		p.next = (p.next + 1) % len(p.sess)
		if !p.invalid[s.Value] {
			return s, true
		}
	}
	return Session{}, false
}

func (p *SessionPool) Retire(value string) {
	p.mu.Lock()
	p.invalid[value] = true
	p.mu.Unlock()
}

func (p *SessionPool) Stats() (int, int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	alive := 0
	for _, s := range p.sess {
		if !p.invalid[s.Value] {
			alive++
		}
	}
	return alive, len(p.sess)
}
