.PHONY: build install service

BIN=shiri-linux

build:
	go build -o $(BIN) ./cmd/shiri-linux

install: build
	mkdir -p $(HOME)/.local/bin
	cp $(BIN) $(HOME)/.local/bin/$(BIN)

service:
	mkdir -p $(HOME)/.config/systemd/user
	cp packaging/shiri-linux.service $(HOME)/.config/systemd/user/
	systemctl --user daemon-reload
	@echo "Enable with: systemctl --user enable --now shiri-linux.service"


