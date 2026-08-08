#!/bin/bash
# EasyTransfer — installation en une commande.
#
#   curl -fsSL https://raw.githubusercontent.com/Enzoblgz/easytransfer/main/install.sh | bash
#
# Installe adb si besoin, récupère le code, crée un lanceur double-cliquable,
# et met en place la détection au branchement. Rejouable sans risque.

set -euo pipefail

REPO="https://github.com/Enzoblgz/easytransfer.git"
DEST="${EASYTRANSFER_HOME:-$HOME/Applications/EasyTransfer}"
APPS="$HOME/Applications"

say()  { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '\n  \033[31m✗ %s\033[0m\n\n' "$1" >&2; exit 1; }

[ "$(uname)" = "Darwin" ] || die "EasyTransfer ne fonctionne que sur macOS."

say "EasyTransfer — installation"

# ---------------------------------------------------------------- 1. Python
# macOS fournit python3 ; on n'installe rien de plus, aucune dépendance.
command -v python3 >/dev/null 2>&1 \
  || die "python3 est introuvable. Installe les outils Xcode : xcode-select --install"
ok "python3 $(python3 -c 'import platform;print(platform.python_version())')"

# ------------------------------------------------------------------- 2. adb
if command -v adb >/dev/null 2>&1; then
  ok "adb déjà installé"
else
  if command -v brew >/dev/null 2>&1; then
    warn "adb manquant — installation via Homebrew (ça peut prendre une minute)"
    brew install --quiet android-platform-tools
    ok "adb installé"
  else
    die "adb est nécessaire, et Homebrew est introuvable.
     Installe Homebrew depuis https://brew.sh puis relance cette commande,
     ou installe adb toi-même : brew install android-platform-tools"
  fi
fi

# -------------------------------------------------------- 3. ffmpeg (option)
if command -v ffmpeg >/dev/null 2>&1; then
  ok "ffmpeg présent — les vignettes vidéo seront affichées"
else
  warn "ffmpeg absent : tout marche, mais les vidéos auront une icône générique"
  warn "pour les activer plus tard :  brew install ffmpeg"
fi

# -------------------------------------------------------------- 4. le code
mkdir -p "$APPS"
if [ -d "$DEST/.git" ]; then
  git -C "$DEST" pull --ff-only --quiet && ok "mis à jour dans $DEST"
else
  git clone --quiet "$REPO" "$DEST" && ok "installé dans $DEST"
fi
chmod +x "$DEST/easytransfer" "$DEST/install-watcher.sh" "$DEST/install.sh" 2>/dev/null || true

# ------------------------------------------------- 5. lanceur double-cliquable
cat > "$APPS/EasyTransfer.command" <<EOF
#!/bin/bash
cd "$DEST" || exit 1
exec ./easytransfer
EOF
chmod +x "$APPS/EasyTransfer.command"
ok "lanceur créé : ~/Applications/EasyTransfer.command (double-clic)"

# ------------------------------------------------ 6. détection au branchement
if "$DEST/install-watcher.sh" install >/dev/null 2>&1; then
  ok "détection au branchement active"
else
  warn "détection au branchement non installée (facultatif) :"
  warn "  $DEST/install-watcher.sh install"
fi

say "C'est prêt."
cat <<EOF
  Sur le téléphone, une seule chose à faire — activer le débogage USB :
    Paramètres › À propos du téléphone › taper 7 fois sur « Numéro de build »,
    puis Paramètres › Options pour les développeurs › Débogage USB.

  Ensuite : branche le téléphone, accepte « Autoriser le débogage USB »,
  et une fenêtre te proposera d'ouvrir EasyTransfer.

  Lancer à la main   : double-clic sur ~/Applications/EasyTransfer.command
  Tout désinstaller  : $DEST/install-watcher.sh uninstall && rm -rf "$DEST"

EOF
