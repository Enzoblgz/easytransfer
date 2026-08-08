# EasyTransfer

**Un Finder pour ton téléphone Android, branché en USB sur un Mac.**

macOS ne parle à Android qu'en MTP : lent, capricieux, et une bonne partie du
stockage reste invisible. EasyTransfer passe par **adb**, qui voit tout le
système de fichiers — y compris les photos WhatsApp que le Finder ne montre pas.

Parcours tes dossiers, vois toutes tes photos dans une seule galerie, et
transfère-les sur ton Mac en un clic.

## Installation

Une seule commande à coller dans le Terminal :

```bash
curl -fsSL https://raw.githubusercontent.com/Enzoblgz/easytransfer/main/install.sh | bash
```

Elle installe `adb` si besoin, met le code dans `~/Applications/EasyTransfer`,
crée un lanceur double-cliquable et active la détection au branchement.
Rejouable sans risque : relance-la pour mettre à jour.

> Tu préfères lire avant d'exécuter ? [Le script fait 80 lignes](install.sh).
> Sinon, à la main : `git clone https://github.com/Enzoblgz/easytransfer.git && cd easytransfer && ./easytransfer`

**Rien à installer côté Python** : macOS fournit déjà tout ce qu'il faut,
l'app n'a aucune dépendance.

### Une seule chose à faire sur le téléphone

Activer le **débogage USB**, sinon le Mac ne pourra pas lire le téléphone :

1. *Paramètres › À propos du téléphone* → taper **7 fois** sur « Numéro de build »
2. *Paramètres › Options pour les développeurs* → activer **Débogage USB**
3. Brancher le câble, puis accepter « Autoriser le débogage USB » sur l'écran

## Lancer

**Branche simplement le téléphone** : une fenêtre s'ouvre et propose
« Ouvrir EasyTransfer ».

À la main : double-clic sur `~/Applications/EasyTransfer.command`. Le navigateur
s'ouvre sur `http://127.0.0.1:8777`, et `Ctrl-C` arrête le serveur.

Si le téléphone n'est pas reconnu, `adb devices` doit afficher une ligne
`device` — et non `unauthorized` (dans ce cas, déverrouille et accepte l'invite).

## Désinstaller

```bash
~/Applications/EasyTransfer/install-watcher.sh uninstall
rm -rf ~/Applications/EasyTransfer ~/Applications/EasyTransfer.command
rm -rf ~/.cache/easytransfer ~/.config/easytransfer
```

## Ce que ça fait

- **Trois vues** : Icônes, Liste, et **Galerie** — la galerie balaie tous les
  sous-dossiers d'un coup et trie du plus récent au plus ancien. Sur
  « Internal storage » ça te donne toutes les photos et vidéos du téléphone dans
  une seule grille — **y compris celles de WhatsApp, Instagram et Telegram**, qui
  vivent sous `Android/media/` et n'apparaissent dans aucun dossier classique.
- **Toute la galerie** : premier favori de la barre latérale — toutes les photos et
  vidéos du téléphone dans une seule grille, sans passer par Screenshots, puis
  Camera, puis WhatsApp.
- **Transfert en 1 clic** : le bouton `↓` sur une vignette, ou `Transférer tout N`
  pour tout le dossier / la sélection. La destination exacte est **écrite en bas à
  droite** en permanence, et un transfert en cours s'**annule** depuis la notification.
- **Sélection** façon Finder : clic, `cmd`-clic, `shift`-clic, `cmd`+`A`.
- **Aperçu plein écran** : barre d'espace (comme dans le Finder), le bouton 👁 ou
  un double-clic. Flèches ← → pour naviguer, `esc` pour fermer. Les vidéos se
  lisent directement depuis le téléphone, sans copie préalable.
- Recherche, fil d'Ariane, historique avant/arrière, thème clair/sombre.

## Pourquoi c'est rapide

Trois choses, parce que l'USB est le goulot d'étranglement :

1. **Vignettes photo par l'EXIF.** Un JPEG d'appareil photo embarque déjà sa
   propre miniature ~512 px dans ses premiers kilo-octets. On lit 128 Ko au lieu
   de rapatrier 4 Mo : ~130 ms au lieu de plusieurs secondes.
2. **Vignettes vidéo par fichier creux.** L'index d'un MP4 (`moov`) est souvent
   écrit à la *fin*. On récupère le début + la fin, on les écrit aux bons
   offsets dans un fichier creux de la taille d'origine, et ffmpeg décode
   normalement. Une vidéo DJI de 598 Mo coûte ~14 Mo de transfert.
3. **Chargement paresseux + cache.** Une vignette n'est demandée que quand la
   tuile approche de l'écran, et le résultat est gardé dans
   `~/.cache/easytransfer/`. Les grosses vidéos sont lues par plages, donc
   l'aperçu démarre sans copier le fichier entier.

## Où atterrissent les fichiers

La destination réelle est affichée **en bas à droite**, en permanence, et se
change avec **Modifier…** (sélecteur de dossier natif). Trois cases à côté :

| Case | Effet |
|---|---|
| **sous-dossier** | recrée le nom du dossier du téléphone dans la destination (`DCIM_Camera`) |
| **demander à chaque fois** | ouvre le sélecteur de dossier à chaque transfert |
| **vider le téléphone** | ⚠️ **décharge réelle** : supprime l'original après copie |

Le réglage est retenu dans `~/.config/easytransfer/config.json`.

### « Vider le téléphone » — comment c'est sécurisé

C'est la seule chose destructive de l'app, donc elle est prudente par construction :

1. le fichier est copié sur le Mac ;
2. sa taille locale est comparée à la taille sur le téléphone, **à l'octet près** ;
3. la suppression n'a lieu **que** si les deux correspondent exactement.

Au moindre doute — copie incomplète, original illisible, transfert annulé — **l'original
est conservé** et le fichier est signalé comme « copié, gardé sur le téléphone ». Mieux
vaut un doublon qu'une photo qui n'existe plus nulle part. L'espace libéré est affiché
à la fin. Activer la case demande une confirmation explicite.

## Détection automatique

Un petit veilleur tourne en fond et surveille le branchement du téléphone.
Dès qu'il le voit, il affiche une fenêtre : **Plus tard** / **Ouvrir EasyTransfer**.

```bash
~/dev/easytransfer/install-watcher.sh            # installer + démarrer
~/dev/easytransfer/install-watcher.sh status     # est-ce qu'il tourne ?
~/dev/easytransfer/install-watcher.sh uninstall  # tout retirer
```

C'est un LaunchAgent (`~/Library/LaunchAgents/fr.bellenguez.easytransfer.plist`) :
il redémarre tout seul s'il plante, et repart à chaque ouverture de session.
Coût mesuré : **0,3 % de CPU, 15 Mo**.

macOS n'a pas d'événement launchd pour « un périphérique USB est apparu », donc
le veilleur interroge `adb` toutes les 3 secondes — c'est ce que l'app utilise
de toute façon.

Deux détails pensés pour éviter les pièges :

- Si le téléphone est branché mais **non autorisé** (l'erreur la plus fréquente),
  il te le dit au bout d'une vingtaine de secondes au lieu de rester muet.
- La fenêtre s'affiche **même si le serveur tourne déjà** — sans ça, comme rien
  n'arrête le serveur, la détection se serait éteinte toute seule après la
  première utilisation.

Pour tester sans rien brancher : `python3 watcher.py --test`.
Journal : `~/.cache/easytransfer/watcher.log`.

## Réglages

| Variable | Défaut | Rôle |
|---|---|---|
| `EASYTRANSFER_DEST` | `~/Downloads/Phone` | où atterrissent les transferts |
| `EASYTRANSFER_PORT` | `8777` | port local |

Les vignettes (et les photos déjà ouvertes en plein écran) sont gardées dans
`~/.cache/easytransfer/`. La taille s'affiche au démarrage ; pour repartir de
zéro : `~/dev/easytransfer/easytransfer --clear-cache`.

## Limites connues

- Lecture seule **sauf si « vider le téléphone » est coché** — c'est la seule
  option qui supprime quoi que ce soit, et elle est décochée par défaut.
- `/sdcard/Android/` est ignoré (stockage privé des apps, illisible sans root).
- Les fichiers `.trashed-*` (corbeille Android) sont masqués.
- Une seule vignette vidéo échoue si le MP4 est illisible en partiel — la tuile
  retombe alors sur une icône générique.
