<#
.SYNOPSIS
	Build, test, package, and optionally publish the Seren Memory VS Code extension.

.DESCRIPTION
	Runs in SerenMemoryVSCode/ (where package.json lives).

	Steps:
	  1. npm install      - ensure deps are up to date
	  2. npm test         - vitest unit tests (fail fast on error)
	  3. npm run build    - production bundle via esbuild
	  4. vsce package     - produces seren-memory-<version>.vsix
	  5. vsce publish     - only when -Publish is passed

.PARAMETER Publish
	When set, publishes to the VS Code Marketplace after packaging.
	Requires VSCE_PAT env var (Personal Access Token) to be set.

.PARAMETER SkipTests
	Skip the vitest run (useful when you've just run tests separately).

.EXAMPLE
	.\publish.ps1
	.\publish.ps1 -Publish
	.\publish.ps1 -SkipTests -Publish
#>
[CmdletBinding()]
param(
	[switch]$Publish,
	[switch]$SkipTests
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# -- helpers ------------------------------------------------------------------

function Step([string]$msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Die([string]$msg)  { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# -- guard: must run from SerenMemoryVSCode/ -----------------------------------

if (-not (Test-Path "package.json")) {
	Die "Run this script from the SerenMemoryVSCode/ directory (package.json not found here)."
}

# -- 1. install deps -----------------------------------------------------------

Step "Installing dependencies"
npm install
if ($LASTEXITCODE -ne 0) { Die "npm install failed." }

# -- 2. tests ------------------------------------------------------------------

if (-not $SkipTests) {
	Step "Running unit tests"
	npm test
	if ($LASTEXITCODE -ne 0) { Die "Tests failed. Fix them before packaging." }
} else {
	Write-Host "  (tests skipped)" -ForegroundColor Yellow
}

# -- 3. production build -------------------------------------------------------

Step "Building production bundle"
npm run build -- --production
if ($LASTEXITCODE -ne 0) { Die "Build failed." }

# -- 4. package ----------------------------------------------------------------

Step "Packaging extension (.vsix)"
npm run package
if ($LASTEXITCODE -ne 0) { Die "vsce package failed." }

$vsix = Get-ChildItem -Filter "*.vsix" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $vsix) { Die "No .vsix file found after packaging." }
Write-Host "  Packaged: $($vsix.Name)" -ForegroundColor Green

# -- 5. publish ----------------------------------------------------------------

if ($Publish) {
	if (-not $env:VSCE_PAT) {
		Die "VSCE_PAT environment variable is not set. Set it to your VS Code Marketplace Personal Access Token."
	}
	Step "Publishing to VS Code Marketplace"
	npx vsce publish --pat $env:VSCE_PAT
	if ($LASTEXITCODE -ne 0) { Die "vsce publish failed." }
	Write-Host "  Published successfully." -ForegroundColor Green
} else {
	Write-Host "`nTo install locally:" -ForegroundColor Cyan
	Write-Host "  code --install-extension $($vsix.Name)"
	Write-Host "`nTo publish to the Marketplace:" -ForegroundColor Cyan
	Write-Host "  `$env:VSCE_PAT = '<your-token>'; .\publish.ps1 -Publish"
}
