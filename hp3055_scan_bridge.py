#!/usr/bin/env python3
"""
Pont "bouton Scan" HP LaserJet 3050/3052/3055/3390/3392 -> scanservjs, sans passer par le
logiciel HP d'epoque ni une VM.

Architecture (voir PROTOCOL.md pour le detail du protocole propriétaire et son historique) :
1. S'enregistre comme plusieurs destinations de scan aupres de l'imprimante (une par profil
   couleur/resolution ci-dessous), via l'API HTTP/XML proprietaire du panneau.
2. Poll notifications.xml en boucle pour detecter la selection de l'une de ces destinations
   au panneau + l'appui sur le bouton "Start".
3. Des detection, delegue l'acquisition reelle du scan a scanservjs via son API HTTP locale
   (POST /api/v1/scan) plutot que de reproduire nous-memes la sequence SNMP de declenchement
   et de recuperation du JPEG brut.

Ce choix d'architecture n'est pas arbitraire : une premiere version de ce pont reproduisait
elle-meme tout le protocole SNMP de bout en bout (voir l'historique git), mais produisait des
scans corrompus sur des documents denses (texte serre, photos detaillees) — tres probablement
un defaut du firmware de l'imprimante dans ce chemin "push" specifique, puisque scanservjs
(qui utilise HPLIP/SANE, un chemin "pull" completement distinct pour le meme materiel) ne
montre jamais ce probleme. Ce pont se contente donc de detecter l'evenement bouton (ce que ni
SANE ni scanservjs ne savent faire pour ce protocole proprietaire) et laisse scanservjs faire
le travail d'acquisition, dont la fiabilite est deja eprouvee.

Configuration via variables d'environnement (voir README.md) :
  PRINTER_IP, HOST_ID, SCANSERVJS_URL, SCANSERVJS_DEVICE_ID
"""

import json
import os
import socket
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

PRINTER_IP = os.environ.get("PRINTER_IP", "192.168.1.2")
HOST_ID = os.environ.get("HOST_ID", "MYPC")  # doit correspondre au nom NetBIOS reel de la
# machine (le firmware semble le resoudre par NBNS avant d'accepter le scan).

SCANSERVJS_URL = os.environ.get("SCANSERVJS_URL", "http://127.0.0.1:8080")
SCANSERVJS_DEVICE_ID = os.environ.get("SCANSERVJS_DEVICE_ID", "")  # requis, voir README.md
# (visible via `curl $SCANSERVJS_URL/api/v1/context`, champ devices[].id)

# Un bug (ou un comportement inattendu) a ete observe ou une destination qui obtenait le
# DestinationID **0** semblait echouer systematiquement ; non confirme avec certitude apres
# la decouverte du vrai declencheur SNMP (voir PROTOCOL.md). Palliatif conserve par prudence :
# sacrifier le slot 0 a une destination bidon des le demarrage, avant d'enregistrer les
# vraies destinations (seulement si la liste est totalement vide, ex. apres un redemarrage
# de l'imprimante).
SACRIFICE_DISPLAY = f"{HOST_ID}:_slot0_sacrifice"
POLL_INTERVAL_S = 5

# Chaque profil correspond a une destination separee au panneau (affichee "<HOST_ID>:<suffix>"
# — le panneau du 3055 n'affiche que 9 caracteres visibles apres le HostID, d'ou des noms
# courts), et a un jeu de parametres passes tel quel a l'API scanservjs (voir
# https://github.com/sbs20/scanservjs, endpoint POST /api/v1/scan). "mode" doit etre une des
# options exposees par le backend SANE du device (verifier avec `--mode` dans la reponse de
# /api/v1/context) : pour HPLIP/hpaio, "Lineart"|"Gray"|"Color". Le pipeline choisit le format
# de sortie ; voir la liste complete des pipelines dans /api/v1/context, champ
# devices[].settings.pipeline.options.
PROFILES = [
    {
        "suffix": "COLOR200",
        "params": {"mode": "Color", "resolution": 200},
        "pipeline": "PDF (JPG | @:pipeline.medium-quality)",
    },
    {
        "suffix": "GRAY300",
        "params": {"mode": "Gray", "resolution": 300},
        "pipeline": "PDF (JPG | @:pipeline.medium-quality)",
    },
    {
        "suffix": "BW300",
        "params": {"mode": "Lineart", "resolution": 300},
        "pipeline": "PDF (JPG | @:pipeline.medium-quality)",
    },
]

NS = {"pls": "http://www.hp.com/schemas/imaging/pls/dev/1.0"}


def log(msg):
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def profile_display(profile: dict) -> str:
    return f"{HOST_ID}:{profile['suffix']}"


def http_get(path):
    url = f"http://{PRINTER_IP}{path}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read()


def dechunk(body: bytes) -> bytes:
    out = []
    i = 0
    while True:
        j = body.index(b"\r\n", i)
        size = int(body[i:j], 16)
        if size == 0:
            break
        out.append(body[j + 2 : j + 2 + size])
        i = j + 2 + size + 2
    return b"".join(out)


def raw_http_get(path, extra_headers: list):
    """Requete HTTP construite a la main, pour reproduire a l'octet pres le format observe
    dans la capture d'origine (en-tetes en MAJUSCULES, Host:localhost:3910, User-Agent:hp
    Proxy/3.0) : l'imprimante semble s'en servir pour reconnaitre un client "hp Proxy"
    legitime avant d'autoriser un scan vers cette destination."""
    lines = [f"GET {path} HTTP/1.1"] + extra_headers + ["", ""]
    request = ("\r\n".join(lines)).encode("ascii")
    with socket.create_connection((PRINTER_IP, 80), timeout=10) as s:
        s.sendall(request)
        s.settimeout(10)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
            if data.endswith(b"0\r\n\r\n"):
                break
    header_end = data.index(b"\r\n\r\n")
    body = data[header_end + 4 :]
    if b"chunked" in data[:header_end].lower():
        body = dechunk(body)
    return body


def http_post(path, body: bytes, headers: dict):
    url = f"http://{PRINTER_IP}{path}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


def list_destinations():
    xml_bytes = http_get("/hp/device/info_scanto_destinations.xml")
    root = ET.fromstring(xml_bytes)
    dests = []
    for d in root.findall(".//pls:ScanToDestination", NS):
        host_id = d.findtext("pls:HostID", "", NS)
        display = d.findtext("pls:DeviceDisplay", "", NS)
        dests.append((host_id, display))
    return dests


def add_destination(display_name: str):
    body_str = f"AddScanToDest_1={HOST_ID}^{display_name}^DestFolder".replace("^", "%5e").replace(":", "%3a")
    body = body_str.encode("ascii")
    headers = {
        "User-Agent": "ScanDestSetup",
        "Content-Length": str(len(body)),
        "Connection": "Keep-Alive",
        "Cache-Control": "no-cache",
    }
    http_post("/hp/device/set_config.html", body, headers)
    log(f"Destination enregistree : {HOST_ID}^{display_name}")


def ensure_destinations_registered():
    existing = list_destinations()
    if not existing:
        # L'imprimante vient probablement de redemarrer (destinations effacees) : le
        # premier DestinationID attribue sera 0 (voir note SACRIFICE_DISPLAY plus haut).
        add_destination(SACRIFICE_DISPLAY)
        existing = list_destinations()
    for profile in PROFILES:
        disp = profile_display(profile)
        if (HOST_ID, disp) in existing:
            continue
        add_destination(disp)


def poll_notifications():
    xml_bytes = raw_http_get(
        "/hp/device/notifications.xml",
        ["CONTENT-LENGTH:0", "HOST:localhost:3910", "USER-AGENT:hp Proxy/3.0"],
    )
    root = ET.fromstring(xml_bytes)
    scan_to = root.find(".//pls:ScanToNotifications", NS)
    if scan_to is None:
        return None
    return {
        "ScanToHostID": scan_to.findtext("pls:ScanToHostID", "", NS),
        "ScanToDeviceDisplay": scan_to.findtext("pls:ScanToDeviceDisplay", "", NS),
    }


def match_profile(notification: dict):
    if not notification or notification["ScanToHostID"] != HOST_ID:
        return None
    for profile in PROFILES:
        if notification["ScanToDeviceDisplay"] == profile_display(profile):
            return profile
    return None


def discover():
    """Reproduit la sequence de requetes qu'un vrai logiciel HP effectue au demarrage avant
    de poller notifications.xml. Hypothese : l'imprimante n'accepte un client comme
    "connecte" pour une destination qu'apres avoir vu cette sequence depuis son IP."""
    for path in (
        "/hp/device/product_information.xml",
        "/hp/device/static.xml",
        "/hp/device/faxlog.xml",
        "/hp/device/settings_fax_tasks.xml",
        "/hp/device/alerts.xml",
    ):
        try:
            http_get(path)
        except Exception as e:
            log(f"discover() erreur sur {path} (ignoree) : {e!r}")


def trigger_scanservjs_scan(profile: dict):
    """Demande a scanservjs d'effectuer l'acquisition reelle, via son API HTTP locale (la
    meme que son interface web utilise). Voir docstring du module pour le choix de deleguer
    ici plutot que de reproduire la sequence SNMP nous-memes."""
    if not SCANSERVJS_DEVICE_ID:
        raise RuntimeError("SCANSERVJS_DEVICE_ID non configure (voir README.md)")
    body = {
        "params": {"deviceId": SCANSERVJS_DEVICE_ID, **profile["params"]},
        "pipeline": profile["pipeline"],
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{SCANSERVJS_URL}/api/v1/scan",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Le scan physique (mecanique) + la conversion prennent facilement 30-60s pour un
    # document dense a 300dpi : timeout genereux, scanservjs ne repond qu'une fois termine.
    with urllib.request.urlopen(req, timeout=120) as r:
        response = json.loads(r.read())
    saved = response.get("file", {}).get("fullname", "?")
    log(f"scan effectue par scanservjs : {saved}")


def main():
    profiles_desc = ", ".join(f"{HOST_ID}:{p['suffix']} ({p['params']['mode']})" for p in PROFILES)
    log(f"demarrage, cible {PRINTER_IP}, destinations : {profiles_desc}")
    log(f"delegue a scanservjs : {SCANSERVJS_URL} (device={SCANSERVJS_DEVICE_ID!r})")
    ensure_destinations_registered()
    discover()
    log("sequence de decouverte effectuee")
    cycle = 0
    last_matched_suffix = None  # front montant seulement : eviter de re-traiter le meme job
    while True:
        try:
            if cycle % 12 == 0:  # ~ toutes les 60s avec POLL_INTERVAL_S=5
                discover()
                ensure_destinations_registered()  # au cas ou l'imprimante aurait redemarre
                # (efface ses destinations) pendant que ce pont continue de tourner
            n = poll_notifications()
            matched = match_profile(n)
            matched_suffix = matched["suffix"] if matched else None
            is_new_trigger = matched_suffix is not None and matched_suffix != last_matched_suffix
            last_matched_suffix = matched_suffix
            if is_new_trigger:
                log(f"scan detecte pour {profile_display(matched)!r}, delegation a scanservjs...")
                trigger_scanservjs_scan(matched)
        except Exception as e:
            log(f"erreur (ignoree, on continue) : {e!r}")
        cycle += 1
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
