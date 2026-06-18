# Mirrors the two CI matrix legs locally.
# Requires: make (Git for Windows ships it; or: choco install make / scoop install make)
#
# Targets:
#   make test         - Python base install only (.[dev])    - matches CI extras=dev
#   make test-mcp     - Python with MCP extras  (.[dev,mcp]) - matches CI extras=dev,mcp
#   make test-corp    - Python with CORP extras (.[dev,corp]) - matches CI extras=dev,corp
#   make test-corp-mcp - Python with CORP and MCP extras (.[dev,corp,mcp]) - matches CI extras=dev,corp,mcp
#   make test-vscode  - VS Code extension unit tests (vitest)
#   make test-all     - all five in sequence
#   make clean        - remove any leftover venvs
#
# Each Python target creates a fresh isolated venv, runs tests, then removes it.
# Venvs are also gitignored as a belt-and-suspenders safety net.

SHELL        := pwsh.exe
.SHELLFLAGS  := -NoProfile -NonInteractive -Command

PKG_DIR    := SerenMemory
VSCODE_DIR := SerenMemoryVSCode
VENV_BASE  := .venv-base
VENV_MCP   := .venv-mcp
VENV_CORP  := .venv-corp
VENV_CORP_MCP := .venv-corp-mcp

.PHONY: test test-mcp test-corp test-corp-mcp test-vscode

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

test-corp:
	Remove-Item -Recurse -Force $(VENV_CORP) -ErrorAction SilentlyContinue; \
	python -m venv $(VENV_CORP); \
	$$env:SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0'; \
	.\.venv-corp\Scripts\pip.exe install -e "$(PKG_DIR)/.[dev,corp]"; \
	.\.venv-corp\Scripts\python.exe -m pytest $(PKG_DIR)/tests/ -v; \
	$$status=$$LASTEXITCODE; \
	Remove-Item -Recurse -Force $(VENV_CORP) -ErrorAction SilentlyContinue; \
	exit $$status

test-corp-mcp:
	Remove-Item -Recurse -Force $(VENV_CORP_MCP) -ErrorAction SilentlyContinue; \
	python -m venv $(VENV_CORP_MCP); \
	$$env:SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0'; \
	.\.venv-corp-mcp\Scripts\pip.exe install -e "$(PKG_DIR)/.[dev,corp,mcp]"; \
	.\.venv-corp-mcp\Scripts\python.exe -m pytest $(PKG_DIR)/tests/ -v; \
	$$status=$$LASTEXITCODE; \
	Remove-Item -Recurse -Force $(VENV_CORP_MCP) -ErrorAction SilentlyContinue; \
	exit $$status

test-vscode:
	npm --prefix $(VSCODE_DIR) install; \
	npm --prefix $(VSCODE_DIR) test; \
	exit $$LASTEXITCODE

test-all: test test-mcp test-corp test-corp-mcp test-vscode

clean:
	Remove-Item -Recurse -Force $(VENV_BASE), $(VENV_MCP), $(VENV_CORP), $(VENV_CORP_MCP) -ErrorAction SilentlyContinue
