# Protocole "Scan to PC" du HP LaserJet 3050/3052/3055/3390/3392 — reverse engineering

> **Note (2026-10-04)** : depuis cette date, `hp3055_scan_bridge.py` n'utilise plus les
> sections 3/3bis/3ter de ce document pour l'acquisition du scan — il delegue cette partie a
> scanservjs via son API HTTP (voir README.md, section "Pourquoi deleguer a scanservjs ?").
> Seules les sections 1 et 2 (enregistrement de destination, detection de l'appui bouton via
> `notifications.xml`) restent utilisees en pratique. Le reste de ce document est conserve tel
> quel : la sequence SNMP de declenchement direct fonctionne et reste documentee ici pour
> quiconque voudrait l'utiliser sans dependre de scanservjs, ou pour comprendre l'historique
> du diagnostic (section 3ter en particulier).

Capture de reference obtenue en enregistrant une destination de scan depuis le logiciel HP
"Full Solution" (tournant dans une VM Windows 2000, l'installeur d'epoque ne s'installant
pas proprement sur un OS moderne), puis en selectionnant cette destination au panneau du
HP3055 et en lancant un scan, tout en capturant le trafic reseau (port miroir du switch).

Le HP3055 embarque un serveur web (`Virata-EmWeb/R6_0_1`) exposant une API HTTP/XML
proprietaire HP ("PML"/imaging schema `http://www.hp.com/schemas/imaging/pls/dev/1.0`) sur le
port 80. Aucune authentification n'est requise sur le reseau local.

## 1. Enregistrement d'une destination (une fois, depuis la config du logiciel HP)

Requete observee (User-Agent distinctif `ScanDestSetup`) :

```
POST /hp/device/set_config.html HTTP/1.1
User-Agent: ScanDestSetup
Host: 192.168.1.2
Content-Length: 241
Connection: Keep-Alive
Cache-Control: no-cache

AddScanToDest_1=MYPC^MYPC:Email Doc^DestFolder
&AddScanToDest_2=MYPC^MYPC:Email Pic^DestFolder
&AddScanToDest_3=MYPC^MYPC:File^DestFolder
&AddScanToDest_4=MYPC^MYPC:SCAN^DestFolder
```
(`^` = `%5e`, `:` = `%3a` url-encodes ; corps reel sur une seule ligne, mis en forme ici pour
lisibilite). Format de chaque entree `AddScanToDest_N` : `<HostID>^<DeviceDisplay>^<ScanToType>`.
- `HostID` : nom de la machine hote (le nom NetBIOS reel de la machine — voir section 3bis,
  le firmware semble le resoudre par NBNS).
- `DeviceDisplay` : libelle affiche au panneau du 3055 dans la liste des destinations.
- `ScanToType` : toujours `DestFolder` observe ici (autres valeurs possibles non testees, ex
  `DestEmail` ?).

Reponse : `200 OK`, corps XML `<Resets><ResetType>0</ResetType></Resets>` (chunked).

On peut relire la liste des destinations enregistrees avec :
```
GET /hp/device/info_scanto_destinations.xml HTTP/1.1
```
=> XML `<ScanToDestinations>` listant chaque `<ScanToDestination DestinationID="N">` avec
`<HostID>`, `<DeviceDisplay>`, `<ScanToType>`. Les destinations sont **volatiles** : un
redemarrage du 3055 les efface toutes (le vrai logiciel HP les reenregistre a chaque
demarrage).

## 2. Boucle d'attente ("le logiciel HP guette le bouton du panneau")

Le proxy/service HP local (vu comme client HTTP avec `User-Agent: hp Proxy/3.0`,
`Host: localhost:3910` — indique un service local sur le PC qui ecoute en `:3910` et relaie
vers le 3055) **poll toutes les ~6 secondes** :

```
GET /hp/device/notifications.xml HTTP/1.1
CONTENT-LENGTH:0
HOST:localhost:3910
USER-AGENT:hp Proxy/3.0
```

Reponse XML `<Notifications>` avec plusieurs blocs. Le bloc pertinent :

```xml
<ScanToNotifications>
  <ScanToDeviceDisplay></ScanToDeviceDisplay>
  <ScanToHostID></ScanToHostID>
  <ScanToNotSetup>0</ScanToNotSetup>
  <ADFLoaded>0</ADFLoaded>
</ScanToNotifications>
```

**A l'etat de repos, `ScanToDeviceDisplay` et `ScanToHostID` sont vides.** Des que l'utilisateur
choisit une destination au panneau et lance le scan, LA MEME requete de polling recoit une
reponse ou ces deux champs sont remplis avec les valeurs exactement enregistrees a l'etape 1
(ex: `<ScanToDeviceDisplay>MYPC:Email Doc</ScanToDeviceDisplay>` /
`<ScanToHostID>MYPC</ScanToHostID>`). C'est le signal "destination choisie" — mais **ce n'est
pas ce qui declenche physiquement le scan**, voir section 3bis.

## 3. Recuperation de l'image scannee — port TCP 8290 (protocole brut, pas HTTP)

Une fois le scan physiquement demarre (section 3bis), le client ouvre une connexion TCP brute
vers `<IP imprimante>:8290` (pas de HTTP du tout). Le serveur envoie directement :

- **Un en-tete binaire proprietaire (~74 octets observes)** : structure exacte non entierement
  decodee, mais on y trouve les valeurs de resolution (XRes/YRes en DPI) et les dimensions en
  pixels de l'image, en clair, peu avant le debut du JPEG.
- **Immediatement suivi du JPEG complet** : marqueur SOI+APP0 `FFD8 FFE0 ... 4A46 4946 00`
  (JFIF), jusqu'au marqueur EOI `FFD9`.

Le delai entre la connexion TCP et le premier octet recu correspond au temps physique du scan
(observe entre ~18s et ~20s). Pas d'autre echange sur ce port : le serveur pousse les donnees
des l'etablissement de la connexion, le client se contente d'ACKer et de fermer une fois la
lecture terminee.

## 3bis. Le vrai declencheur du scan physique : SNMP, pas HTTP

Le point 2 (`notifications.xml`) ne fait que **refleter** l'etat pour l'affichage panneau —
ce n'est PAS ce qui demarre mecaniquement le scan. Le vrai logiciel HP envoie en parallele
une sequence **SNMP v1** (communaute `"internal"`) vers des OID HP privees, capturee en
observant le trafic reseau du logiciel HP pendant un scan reussi (a capturer sur le trafic
du PC client vers l'imprimante, pas uniquement sur l'imprimante elle-meme si le PC qui
capture n'est pas celui qui scanne — un port miroir/SPAN configure sur le bon port de switch
est necessaire pour voir ce trafic depuis une troisieme machine).

Sequence observee (`SetRequest`, OID sous prefixe `.1.3.6.1.4.1.11.2.3.9.4.2.1.2.2.1`) :

| OID (suffixe) | Type       | Valeur observee           | Role probable |
|---|---|---|---|
| `.3.0`  | INTEGER    | **8 = niveaux de gris, 24 = couleur** (confirme sur 6 essais croises, voir tableau ci-dessous) | `objPixelDataType` (nom officiel HPLIP, voir section 3ter) |
| `.2.0`  | Hex-STRING | `XX XX 00 00` repete 2x (XRes puis YRes, little-endian 16 bits utiles) | `objResolution` : `C8 00...`x2 = 200dpi, `2C 01...`x2 = 300dpi, `96 00...`x2 = 150dpi |
| `.16.0` | INTEGER    | 0                         | ? (absent du code HPLIP — specifique au mecanisme "push" panneau, voir 3ter) |
| `.17.0` | INTEGER    | largeur en pixels, varie avec `.2.0` (2478-2480 a 200/300dpi pour du A4) | largeur en pixels, deduite de la resolution (~ DPI x 8.27") — absent du code HPLIP |
| `.50.0` | INTEGER    | 8409 (gris) / 8448 (couleur 300dpi) / 8409 (couleur 200dpi) | taille/buffer attendu, varie avec mode+resolution — absent du code HPLIP |
| `.76.0` | INTEGER    | 0                         | ? (absent du code HPLIP) |
| `.4.0`  | INTEGER    | **toujours 6** dans tous nos essais (gris, couleur, 150/200/300dpi) | `objCompression` (nom officiel HPLIP) — **6 = JPEG**, confirme par source (voir 3ter) |
| `.5.0`  | INTEGER    | **jamais regle par notre bridge avant le 2026-10-04** | `objCompressionFactor` (0-100) — **champ manquant identifie comme cause probable de corruption**, voir section 3ter |
| `.53.0` | Hex-STRING | `33 33 02 00`             | ? (absent du code HPLIP) |
| `.54.0` | INTEGER    | 1                         | ? (absent du code HPLIP) |
| `.12.0` | INTEGER    | **1→2 = GO**, passe a 5 une fois l'image prete, a remettre a 1 par le client apres recuperation | `objUploadState` (nom officiel HPLIP) |

### Mode scan (.3.0) et resolution (.2.0/.17.0) — confirme par tests croises (2026-10-03)

Six scans tests effectues depuis le vrai logiciel HP (VM), en faisant varier mode et
resolution independamment, destination et format de sortie differents a chaque fois,
capture reseau a chaque essai :

| Test | Format sortie (PC) | `.3.0` | `.2.0`/`.17.0` (DPI deduit) | Image obtenue (verifiee) |
|---|---|---|---|---|
| JOSE:File | — | 24 | 150dpi, 2544px | couleur |
| JOSE:TEST_JPG | JPG | 24 | 300dpi, 2528px | couleur (confirme par logiciel : "millions of colors" 200dpi affiche, mais .2.0 montre 300 — le logiciel demande une resolution native puis sous-echantillonne lui-meme en local) |
| JOSE:TEST_1B (wizard A4/BW/300dpi) | TIFF | 8 | 300dpi, 2480px | **A4 BW confirme** |
| JOSE:TEST_CO_J (couleur 200dpi A4) | JPG | 24 | 200dpi, 2478px | couleur |
| JOSE:TEST_CO_P (memes parametres) | PDF | 24 | 200dpi, 2478px | couleur — **SNMP et flux port 8290 identiques a l'octet pres au test JPG precedent** |
| JOSE:TEST_GR_P / TEST_GR_J (grayscale 300dpi) | PDF puis JPG | 8 | 300dpi, 2480px | **niveaux de gris confirme** (pas du noir/blanc 1-bit pur) |

**Conclusions etablies :**
- `.3.0` est le seul vrai selecteur de mode cote imprimante, et il n'a que **2 valeurs
  possibles** : `8` et `24`. Pas de 3e valeur pour "noir et blanc" distinct de "niveaux de
  gris" — les deux rendus (`TEST_1B` et `TEST_GR_*`) donnent exactement la meme sequence
  SNMP (`.3.0=8`, memes autres champs). **La distinction grayscale vs BW/1-bit est donc un
  post-traitement logiciel (seuillage) applique par le PC apres reception du JPEG en
  niveaux de gris**, pas un reglage materiel de l'imprimante.
- `.2.0` encode directement le DPI demande (XRes/YRes identiques, codes sur les 2 premiers
  octets de chaque mot de 4 octets, le reste a 0), et `.17.0` est la largeur en pixels
  coherente pour une page A4 a cette resolution. Les deux valeurs bougent ensemble et sont
  independantes du mode (`.3.0`).
- **Le format de sortie choisi dans le logiciel HP (JPG/PDF/TIFF/...) n'a aucune influence
  sur le trafic reseau** : capture rigoureusement identique (SNMP et donnees port 8290)
  entre un essai "JPG" et un essai "PDF" avec les memes reglages mode/resolution/destination.
  L'imprimante envoie **toujours** du JPEG brut sur le port 8290 ; la conversion vers
  PDF/TIFF/etc. est faite localement par le logiciel PC apres reception.
- **Le niveau de compression JPEG (qualite/taille de fichier) n'est pas non plus pilote par
  SNMP.** Deux scans consecutifs, memes reglages (gris, 300dpi, meme destination), seule la
  compression changee dans le logiciel HP ("default" vs "smallest file size") : sequences
  SNMP **rigoureusement identiques a l'octet pres** (seul le nonce aleatoire `.1.1.1.25.0`
  differe, sans rapport). Pourtant le volume reellement transfere sur le port 8290 differe
  nettement : ~99 Ko (default) vs ~83 Ko (smallest file size), soit ~16% de moins. La
  compression est donc **decidee par l'imprimante elle-meme** (elle sait produire un JPEG
  plus ou moins compresse) mais **pas via un des OID connus** — soit via un champ encore
  marque "?" dans le tableau ci-dessus (candidats : `.53.0` ou `.76.0`, jamais vus varier
  dans nos essais mais pas testes isolement pour la compression), soit via l'en-tete binaire
  de 74 octets avant le JPEG sur le port 8290 (non decode), soit via un mecanisme totalement
  different qu'on n'a pas encore identifie. **Non elucide — a creuser.**

Il y a aussi un champ `.1.3.6.1.4.1.11.2.3.9.4.2.1.1.1.25.0` (Hex-STRING 16 octets) qui
alterne entre zero et des valeurs a priori aleatoires plusieurs fois avant le `GO` — role non
elucide (heartbeat ? nonce ?). **Pas necessaire** : le scan fonctionne sans le reproduire.

**Sequence minimale qui fonctionne** (voir `hp3055_scan_bridge.py`) : poser les 9 valeurs de
configuration ci-dessus, puis `SET .12.0 = 2` pour demarrer physiquement le scan. Le moteur
demarre alors avec un delai mecanique de ~15-20s avant que les donnees soient disponibles sur
le port 8290 (section 3). Une fois l'image recuperee, remettre `.12.0 = 1` (sinon l'OID reste
bloque et un nouveau `SET .12.0 = 2` est refuse avec `noSuchName` — la transition n'est
acceptee que depuis l'etat 1).

Outils pour experimenter manuellement : `snmpget`/`snmpset` (paquet Debian/Ubuntu `snmp`,
net-snmp), ex. :
```
snmpset -v1 -c internal <IP imprimante> .1.3.6.1.4.1.11.2.3.9.4.2.1.2.2.1.12.0 i 2
```

Point important : cette sequence SNMP ne semble **pas liee a une destination particuliere**
(rien dans les paquets SNMP n'identifie HostID/DeviceDisplay) — elle pilote juste le moteur de
scan lui-meme. Le routage vers la bonne destination HTTP (`notifications.xml`) et le port 8290
comme canal de recuperation restent lies au mecanisme de destination des sections 1-2.

## 3ter. Corruption JPEG sur documents denses a 300dpi — diagnostic via source HPLIP (2026-10-04)

**Symptome observe** (2026-10-03 soir) : un scan bouton d'une facture texte dense (BW300 ou
GRAY300, ~1.9-2 Mo de JPEG brut, contre 80-180 Ko pour nos tests precedents avec des pages
quasi vides) produit une image correcte sur le haut de la page puis degenere en bruit/
corruption puis en noir pour le reste. **Reproduit a l'identique avec le vrai logiciel HP sur
la VM** (meme destination/sequence SNMP de base), ce qui ecarte un bug du bridge lui-meme —
mais **scanservjs (HPLIP/SANE) scanne le meme document sans aucun probleme**, ce qui ecarte
aussi une limitation materielle generale du scanner.

**Methode de diagnostic** : capture reseau d'un scan scanservjs du meme document (meme reseau,
meme OID SNMP de base `.1.3.6.1.4.1.11.2.3.9.4.2.1.*` — confirmant que SANE/HPLIP utilise EXACTEMENT
le meme mecanisme SNMP+port 8290 que le "ScanToPC" du panneau, pas un protocole different comme
suppose initialement), puis lecture du code source HPLIP (`libsane-hpaio`, paquet
`libsane-hpaio 3.22.10+dfsg0-8.1` installe dans le conteneur `scanservjs-hplip`) pour avoir
la signification **officielle** des OID plutot que de deviner par essais-erreurs. Fichier cle :
[`scan/sane/sclpml.c`](https://github.com/rfabbri/hplip/blob/master/scan/sane/sclpml.c)
(fonction `hpaioPmlAllocateObjects`, ~ligne 336) et
[`scan/sane/common.h`](https://github.com/rfabbri/hplip/blob/master/scan/sane/common.h).

**Table officielle des OID** (extraite du source, prefixe `1.3.6.1.4.1.11.2.3.9.4.2.1` sauf
mention contraire) :

| OID complet | Nom HPLIP | Role |
|---|---|---|
| `.2.2.2.1.0` | `objScannerStatus` | etat general du scanner |
| `.2.2.2.3.0` | `objResolutionRange` | liste des resolutions supportees (chaine ASCII, ex. `(75)x(75),(100)x(100),...,(1200x1200)`) |
| `.1.1.18.0` | `objUploadTimeout` | timeout d'upload (HPLIP envoie `45`, probablement en secondes) |
| `.2.2.1.1.0` | `objContrast` | contraste |
| `.2.2.1.2.0` | `objResolution` | **resolution** (confirme) |
| `.2.2.1.3.0` | `objPixelDataType` | **mode** (confirme : 8=gris, 24=couleur ; HPLIP utilise aussi `1`=lineart) |
| `.2.2.1.4.0` | `objCompression` | **algorithme de compression** : `1`=None, `2`=Default, `3`=MH, `4`=MR, `5`=**MMR (= CCITT G4)**, `6`=**JPEG** |
| `.2.2.1.5.0` | **`objCompressionFactor`** | **facteur de compression JPEG, 0-100** (`MIN_JPEG_COMPRESSION_FACTOR=0`, `MAX=100`) — **jamais regle par notre bridge avant ce correctif** |
| `.2.2.1.6.0` | `objUploadError` | code d'erreur upload |
| `.2.2.1.12.0` | `objUploadState` | **etat machine a etats** (confirme : 1=idle, 2=GO, 5=pret) |
| `.2.2.1.14.0` | `objAbcThresholds` | seuils ABC (auto background control) |
| `.2.2.1.15.0` | `objSharpeningCoefficient` | nettete |
| `.2.2.1.31.0` | `objNeutralClipThresholds` | seuils d'ecretage neutre |
| `.2.2.1.32.0` | `objToneMap` | courbe de tons |
| `.5.1.4.0` | `objCopierReduction` | taux de reduction/agrandissement copieur (100 = 100%, PAS lie a un buffer malgre l'hypothese initiale) |
| `.1.1.1.25.0` | `objScanToken` | jeton de session (d'ou les valeurs pseudo-aleatoires observees) |
| `.2.2.1.75.0` | `objModularHardware` | — |

**Point cle** : `.16.0`, `.17.0`, `.50.0`, `.53.0`, `.54.0` et `.76.0` (que notre bridge regle
depuis le debut, captures sur le tout premier essai reussi via la VM) **n'apparaissent dans
aucun objet PML alloue par HPLIP**. Ce ne sont donc probablement PAS des champs generiques
HP, mais des champs specifiques au mecanisme "push" du bouton panneau (que HPLIP n'utilise
jamais, puisque SANE fonctionne en pull) — PAS une raison de les retirer de notre sequence,
qui reste necessaire pour ce mecanisme-la. Le code HPLIP montre en revanche clairement que
**`.5.0` (CompressionFactor) est un parametre generique**, regle systematiquement par HPLIP
quel que soit le mecanisme de declenchement, et donc manquant chez nous a tort.

**Hypothese de cause racine** : `SAFER_JPEG_COMPRESSION_FACTOR = 10` est un nom tres
explicite dans le source HPLIP (`common.h` ligne 110) — implique qu'un facteur plus eleve
(valeur par defaut/residuelle du firmware si le champ n'est jamais explicitement regle) est
**connu pour etre a risque**. Sans jamais regler `.5.0`, notre bridge (et l'ancien logiciel HP
sur la VM, qui montre la meme corruption) laisse ce facteur a une valeur potentiellement
agressive, qui pourrait faire deborder un buffer interne de l'encodeur JPEG du firmware
(~2005) lorsque le volume de donnees a encoder est important (document dense), alors que les
petites images de nos tests precedents ne declenchaient jamais ce depassement.

**Correctif applique (2026-10-04, NON ENCORE VALIDE PAR UN VRAI SCAN)** : ajout de
`.5.0 = 10 (INTEGER)` a la sequence SNMP des 3 profils JPEG (`COLOR200`, `GRAY300`, `BW300`)
dans `hp3055_scan_bridge.py`, en reprenant la valeur `SAFER_JPEG_COMPRESSION_FACTOR` choisie
par les auteurs de HPLIP. C'est un ajout minimal et conservateur (rien retire de la sequence
existante, qui fonctionne par ailleurs). **A tester explicitement avec un scan dense (facture,
photo detaillee) en BW300 ou GRAY300 une fois ce correctif deploye** — si la corruption
disparait, cause racine confirmee ; sinon, explorer d'autres OID (`.14.0`/`.15.0`/`.31.0`/
`.32.0` : contraste/nettete/seuils, moins probables mais pas testes).

**Piste alternative non retenue pour l'instant** : utiliser directement le mode "vrai N&B"
de HPLIP (`.3.0=1` + `.4.0=5` MMR/G4 au lieu de JPEG) pour le profil BW300, qui eviterait
structurellement le probleme (G4 est un codec different, sans doute plus robuste sur ce
firmware, et plus adapte au texte). Tente une fois manuellement (OID minimales seulement,
sans `.1.1.1.25.0`/`.1.1.18.0`/les lectures prealables que fait HPLIP) : resultat incomplet
(quelques lignes du haut de la page seulement, puis blanc) — la sequence HPLIP complete pour
ce mode n'a pas ete reproduite fidelement. A refaire plus tard en suivant exactement l'ordre
du vrai code HPLIP (y compris les lectures GetRequest et `.1.1.1.25.0`/`.1.1.18.0`) si le
correctif JPEG ci-dessus ne suffit pas pour BW300.

## Ce qu'il reste a clarifier

- Decodage exact de l'en-tete binaire avant le JPEG (offsets precis de largeur/hauteur/dpi/
  autres flags).
- Role exact des OID encore marques "?" (`.16.0`, `.53.0`, `.54.0`, `.76.0`) — absents du
  code HPLIP, donc specifiques au mecanisme "push" du bouton panneau, voir section 3ter.
- **Valider le correctif `.5.0=10` par un vrai scan dense** (voir section 3ter).
- Comportement si plusieurs pages (bac ADF) : une connexion par page ? meme connexion pour
  plusieurs images concatenees ? Non teste.
- Role exact du champ `<StartScan>` (bloc `<StartScanNotifications>` de `notifications.xml`,
  jamais vu a `1` dans nos captures) — peut-etre un mode "scan generique" sans destination
  specifique.
- Confirmer si `ScanToType` accepte d'autres valeurs que `DestFolder`.

## Modeles concernes

Reverse-engineere sur un **HP LaserJet 3055** (firmware `Virata-EmWeb/R6_0_1`). Le
"Full Solution" installer d'epoque couvre aussi les 3050/3052/3390/3392 qui partagent
vraisemblablement le meme moteur/API — non teste sur ces modeles, retours bienvenus.
