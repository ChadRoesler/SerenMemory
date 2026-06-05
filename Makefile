# Mirrors the two CI matrix legs locally.
# Requires: make (Git for Windows ships it; or: choco install make / scoop install make)
#
# Targets:
#   make test       — base install only (.[dev])    — matches CI extras=dev
#   make test-mcp   — with MCP extras  (.[dev,mcp]) — matches CI extras=dev,mcp
#   make test-all   — both in sequence
#   make clean      — remove any leftover venvs
#
# Each target creates a fresh isolated venv, runs tests, then removes it.
# Venvs are also gitignored as a belt-and-suspenders safety net.

SHELL        := pwsh.exe
.SHELLFLAGS  := -NoProfile -NonInteractive -Command

PKG_DIR   := SerenMemory
VENV_BASE := .venv-base
VENV_MCP  := .venv-mcp

.PHONY: test test-mcp test-all clean

test:
	Remove-Item -Recurse -Force $(VENV_BASE) -ErrorAction SilentlyContinue; \
	python -m venv $(VENV_BASE); \
	$$env:SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0'; \
	.\.venv-base\Scripts\pip.exe install -e "$(PKG_DIR)/.[dev]"; \
	.\.venv-base\Scripts\python.exe -m pytest $(PKG_DIR)/tests/ -v; \
	$$status=$$LASTEXITCODE; \
	Remove-Item -Recurse -Force $(VENV_BASE) -ErrorAction SilentlyContinue; \
	exit $$status

test-mcp:
	Remove-Item -Recurse -Force $(VENV_MCP) -ErrorAction SilentlyContinue; \
	python -m venv $(VENV_MCP); \
	$$env:SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0'; \
	.\.venv-mcp\Scripts\pip.exe install -e "$(PKG_DIR)/.[dev,mcp]"; \
	.\.venv-mcp\Scripts\python.exe -m pytest $(PKG_DIR)/tests/ -v; \
	$$status=$$LASTEXITCODE; \
	Remove-Item -Recurse -Force $(VENV_MCP) -ErrorAction SilentlyContinue; \
	exit $$status

test-all: test test-mcp

clean:
	Remove-Item -Recurse -Force $(VENV_BASE), $(VENV_MCP) -ErrorAction SilentlyContinue
