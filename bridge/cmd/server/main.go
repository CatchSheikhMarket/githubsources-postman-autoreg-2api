// cmd/server — тонкая точка входа моста postman2api.
// Вся логика — в internal/app (package app); запускать из корня проекта,
// чтобы .env находился относительно cwd.
package main

import (
	"os"

	app "postman2api/internal/app"
)

func main() {
	args := []string{}
	if len(os.Args) > 1 {
		args = os.Args[1:]
	}
	app.Main(args)
}
