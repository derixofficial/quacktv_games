# Push current folder to a GitHub repo (assumes git is installed)
# Usage: open PowerShell in this folder and run: .\push_to_github.ps1

param(
    [string]$RepoUrl
)

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "Git non è installato o non è nel PATH. Installa Git e riprova."
    exit 1
}

if (-not $RepoUrl) {
    $RepoUrl = Read-Host "Inserisci l'URL del repo GitHub (es. https://github.com/tuo-username/quacktv_games.git)"
}

Write-Host "Inizializzo git nella cartella corrente..."
if (-not (Test-Path .git)) {
    git init
} else {
    Write-Host ".git già presente"
}

git add .
try {
    git commit -m "Initial commit" -q
} catch {
    Write-Host "Commit fallito (forse non ci sono cambiamenti). Procedo comunque."
}

# set main branch
try { git branch -M main } catch {}

# set remote
$existing = git remote | Where-Object { $_ -ne '' }
if ($existing) {
    Write-Host "Remote già presente. Imposto origin a $RepoUrl"
    git remote remove origin 2>$null
}

git remote add origin $RepoUrl

Write-Host "Effettuo push su origin main..."

try {
    git push -u origin main
    Write-Host "Push completato."
} catch {
    Write-Error "Push fallito. Potrebbe essere necessario autenticarsi. Se usi HTTPS, crea un Personal Access Token (PAT) su GitHub e configura il Git Credential Manager oppure usa l'upload via web di GitHub."
}

Write-Host "Se il push fallisce per autenticazione, prova a caricare i file via https://github.com/new -> Upload files."