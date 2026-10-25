// desktop.go — импорт access_token из установленного Postman Desktop.
//
// Desktop хранит сессию в <data dir>/storage/userPartitionData.json:
//
//	v8PartitionsNamespaceMeta.users.activePartition
//	  → v8Partitions[<id>].meta.raw (JSON)
//	    → auth.access_token
//
// Это тот же токен, который приложение шлёт в заголовке x-access-token,
// поэтому отдельный OAuth-логин не нужен, если Desktop уже авторизован.
package app

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

// defaultDataDir — каталог данных Postman Desktop (можно переопределить
// переменной POSTMAN_DATA_DIR).
func defaultDataDir() string {
	if v := os.Getenv("POSTMAN_DATA_DIR"); v != "" {
		return v
	}
	home, _ := os.UserHomeDir()
	switch runtime.GOOS {
	case "darwin":
		return filepath.Join(home, "Library", "Application Support", "Postman")
	case "windows":
		return filepath.Join(os.Getenv("APPDATA"), "Postman")
	default:
		base := os.Getenv("XDG_CONFIG_HOME")
		if base == "" {
			base = filepath.Join(home, ".config")
		}
		return filepath.Join(base, "Postman")
	}
}

// importDesktopSession читает access_token активного пользователя Desktop и
// lastKnownVersion из settings.json (её Postman шлёт в x-app-version —
// хэши инструментов привязаны к версии).
func importDesktopSession(dataDir string) (token, appVersion string, err error) {
	partFile := filepath.Join(dataDir, "storage", "userPartitionData.json")
	raw, err := os.ReadFile(partFile)
	if err != nil {
		return "", "", fmt.Errorf("нет файла сессии Postman Desktop (%s)", partFile)
	}

	var root struct {
		Meta struct {
			Users struct {
				ActivePartition string `json:"activePartition"`
			} `json:"users"`
		} `json:"v8PartitionsNamespaceMeta"`
		Partitions map[string]struct {
			Meta struct {
				Raw string `json:"raw"`
			} `json:"meta"`
		} `json:"v8Partitions"`
	}
	if err := json.Unmarshal(raw, &root); err != nil {
		return "", "", fmt.Errorf("не разобрал %s: %w", partFile, err)
	}

	part, ok := root.Partitions[root.Meta.Users.ActivePartition]
	if !ok {
		return "", "", fmt.Errorf("в %s нет активного пользователя — войди в Postman Desktop", partFile)
	}
	var meta struct {
		Auth struct {
			AccessToken string `json:"access_token"`
		} `json:"auth"`
	}
	if err := json.Unmarshal([]byte(part.Meta.Raw), &meta); err != nil {
		return "", "", fmt.Errorf("не разобрал активную сессию Desktop: %w", err)
	}
	if meta.Auth.AccessToken == "" {
		return "", "", fmt.Errorf("в сессии Postman Desktop нет access_token — войди в приложение")
	}
	return meta.Auth.AccessToken, desktopVersion(dataDir), nil
}

// desktopVersion — lastKnownVersion из settings.json (пустая строка, если нет).
func desktopVersion(dataDir string) string {
	raw, err := os.ReadFile(filepath.Join(dataDir, "storage", "settings.json"))
	if err != nil {
		return ""
	}
	var s struct {
		LastKnownVersion string `json:"lastKnownVersion"`
	}
	if json.Unmarshal(raw, &s) != nil {
		return ""
	}
	return s.LastKnownVersion
}
