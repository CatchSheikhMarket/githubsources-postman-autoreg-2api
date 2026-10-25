// id.go — генерация идентификаторов в формате, который ждут клиенты.
package app

import "crypto/rand"

const idAlphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

// newID — 24-символьный id как у OpenAI (chatcmpl-…, msg_…).
func newID() string {
	b := make([]byte, 24)
	_, _ = rand.Read(b)
	for i, v := range b {
		b[i] = idAlphabet[int(v)%len(idAlphabet)]
	}
	return string(b)
}

// str — безопасное приведение any к string.
func str(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}
