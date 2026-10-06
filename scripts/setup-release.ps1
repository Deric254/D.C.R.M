# One-time release setup. Run from the repo root via setup-release.bat.
# Needs: Node.js, git, and GitHub CLI (gh) logged in with `gh auth login`.
$ErrorActionPreference = "Stop"
$codeRepo = "Deric254/D.C.R.M"
$relRepo  = "Deric254/D.C.R.M-releases"
$conf     = "src-tauri/tauri.release.conf.json"
$dir      = Join-Path $env:USERPROFILE ".dericbi"
$key      = Join-Path $dir "dericbi.key"

function Run($label) { if ($LASTEXITCODE) { throw "$label failed (exit $LASTEXITCODE)" } }

foreach ($t in "node", "npx", "git", "gh") {
  if (-not (Get-Command $t -ErrorAction SilentlyContinue)) { throw "$t is not installed or not on PATH." }
}
gh auth status | Out-Null; Run "gh auth status (run: gh auth login)"
if (-not (Test-Path $conf)) { throw "Run this from the repo root (missing $conf)." }

# 1. Releases repo (public; created with a README because GitHub cannot tag a release in a repo with no commits)
gh repo view $relRepo 2>$null | Out-Null
if ($LASTEXITCODE) {
  gh repo create $relRepo --public --add-readme; Run "create releases repo"
}

# 2. Signing key. Never overwritten: losing it means installed apps can no longer update.
if (-not (Test-Path $key)) {
  New-Item -ItemType Directory -Force $dir | Out-Null
  npx --yes tauri signer generate -w $key --ci; Run "key generation"
}
if (-not (Test-Path "$key.pub")) { throw "Found $key but not $key.pub. Restore the .pub file or move the key aside." }
$pub = (Get-Content -Raw "$key.pub").Trim()

# 3. Public key into the release config
$text = Get-Content -Raw $conf
if ($text -match "REPLACE_WITH_PUBLIC_KEY") {
  [IO.File]::WriteAllText((Resolve-Path $conf), $text.Replace("REPLACE_WITH_PUBLIC_KEY", $pub))
} elseif (-not $text.Contains($pub)) {
  throw "$conf has a different public key than $key.pub. Fix the mismatch by hand; do not overwrite blindly."
}

# 4. GitHub secrets
Get-Content -Raw $key | gh secret set TAURI_SIGNING_PRIVATE_KEY --repo $codeRepo; Run "set TAURI_SIGNING_PRIVATE_KEY"
$existing = gh secret list --repo $codeRepo --json name --jq ".[].name"; Run "list secrets"
if ($existing -notcontains "RELEASES_TOKEN") {
  Write-Host "Create a fine-grained token: Contents read and write, on $relRepo only."
  $sec = Read-Host "Paste RELEASES_TOKEN" -AsSecureString
  $tok = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
  if (-not $tok) { throw "No token entered." }
  $tok | gh secret set RELEASES_TOKEN --repo $codeRepo; Run "set RELEASES_TOKEN"
}

# 5. Commit and push the public key (the push starts the release build)
git add $conf
git diff --cached --quiet
if ($LASTEXITCODE) { git commit -m "Set updater public key"; Run "commit"; git push; Run "push" }
Write-Host "Done. Back up $key somewhere safe."
