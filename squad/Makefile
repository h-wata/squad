SHELL := /bin/bash
PREFIX ?= $(HOME)/.local
BINDIR := $(PREFIX)/bin
TARGET := $(BINDIR)/squad
SOURCE := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))../bin/squad)

.PHONY: install uninstall check

install:
	@mkdir -p $(BINDIR)
	@chmod +x $(SOURCE)
	@ln -sf $(SOURCE) $(TARGET)
	@echo "installed: $(TARGET) -> $(SOURCE)"
	@echo "ensure $(BINDIR) is in your PATH, then: squad ls"

uninstall:
	@rm -f $(TARGET)
	@echo "removed: $(TARGET)"

check:
	@command -v squad >/dev/null && echo "squad: $$(command -v squad)" || echo "squad not in PATH"
	@bash -n '$(SOURCE)' && echo "syntax OK"
