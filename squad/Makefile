SHELL := /bin/bash
PREFIX ?= $(HOME)/.local
BINDIR := $(PREFIX)/bin
TARGET := $(BINDIR)/squad
HERE := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
SOURCE := $(abspath $(HERE)/../bin/squad)
SQUAD_PY := $(HERE)/squad.py

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
	@bash -n $(SOURCE) && echo "bin/squad syntax OK"
	@python3 -c "import ast; ast.parse(open('$(SQUAD_PY)').read())" && echo "squad.py syntax OK"
