#!/usr/bin/env python3
"""
Pont "bouton Scan" HP LaserJet 3050/3052/3055/3390/3392 -> n'importe quel outil de scan
(ex. scanservjs), sans passer par le logiciel HP d'epoque ni une VM.

Reproduit le protocole reverse-engineere (voir PROTOCOL.md) :
1. S'enregistre comme destination de scan aupres de l'imprimante (API HTTP/XML proprietaire).
2. Poll notifications.xml en boucle pour detecter la selection de cette destination au
   panneau + l'appui sur le bouton "Start".
3. Declenche le scan physique via une sequence SNMP (le polling HTTP seul ne suffit pas).
4. Recupere l'image (en-tete proprietaire + JPEG brut) sur le port TCP 8290.
5. Sauvegarde le resultat en PDF dans un dossier de sortie configurable.

Configuration via variables d'environnement (voir README.md) :
  PRINTER_IP, HOST_ID, DEST_DISPLAY, OUTPUT_DIR, SNMP_COMMUNITY
"""

import io
import os
import socket
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from PIL import Image

PRINTER_IP = os.environ.get("PRINTER_IP", "192.168.1.2")
HOST_ID = os.environ.get("HOST_ID", "MYPC")  # doit correspondre au nom NetBIOS reel de la
# machine (le firmware semble le resoudre par NBNS avant d'accepter le scan).
DEST_DISPLAY = os.environ.get("DEST_DISPLAY", "MYPC:AutoScan")

# Un bug (ou un comportement inattendu) a ete observe ou une destination qui obtenait le
# DestinationID **0** semblait echouer systematiquement ; non confirme avec certitude apres
# la decouverte du vrai declencheur SNMP (voir PROTOCOL.md). Palliatif conserve par prudence :
# sacrifier le slot 0 a une destination bidon des le demarrage, avant d'enregistrer la vraie.
SACRIFICE_DISPLAY = f"{HOST_ID}:_slot0_sacrifice"
SCAN_PORT = 8290
POLL_INTERVAL_S = 5

# Le vrai declencheur du scan physique n'est PAS le HTTP notifications.xml (qui ne fait que
# refleter l'etat pour l'affichage panneau) mais une sequence SNMP v1 (communaute "internal"
# par defaut) vers des OID HP privees, capturee depuis un vrai client HP. Sans cette sequence,
# le moteur de scan ne bouge jamais, meme si le panneau affiche "en attente du PC".
SNMP_COMMUNITY = os.environ.get("SNMP_COMMUNITY", "internal")
SNMP_OID_BASE = "1.3.6.1.4.1.11.2.3.9.4.2.1.2.2.1"
SNMP_OID_STATE = f"{SNMP_OID_BASE}.12.0"  # 1=idle, 2=GO (declenche le scan), passe a 5 une
# fois l'image prete a etre recuperee sur le port 8290 ; a remettre a 1 apres recuperation.

# ATTENTION : ces deux valeurs par defaut sont celles qui ont ete testees avec succes, mais
# elles sont INCOHERENTES entre elles (150dpi declare, mais largeur de 2480px qui correspond
# a du A4 a 300dpi — repere a la relecture, jamais corrige/teste). A essayer : RESOLUTION_DPI=300
# avec WIDTH_PX=2480 (coherent), ou RESOLUTION_DPI=150 avec WIDTH_PX=1240 (coherent). Voir
# PROTOCOL.md.
RESOLUTION_DPI = int(os.environ.get("RESOLUTION_DPI", "150"))
WIDTH_PX = int(os.environ.get("WIDTH_PX", "2480"))
_res_hex = f"{RESOLUTION_DPI:04x}"
SNMP_SCAN_PARAMS = [
    # (OID relatif a SNMP_OID_BASE, type snmpset, valeur) - valeurs observees pour un scan
    # couleur ; a affiner si d'autres reglages sont voulus (voir PROTOCOL.md).
    (".3.0", "i", "8"),
    (".2.0", "x", f"{_res_hex}0000{_res_hex}0000"),  # XRes/YRes (2x uint32 BE, 16 bits utiles)
    (".16.0", "i", "0"),
    (".17.0", "i", str(WIDTH_PX)),  # largeur en pixels
    (".50.0", "i", "8409"),
    (".76.0", "i", "0"),
    (".4.0", "i", "6"),
    (".53.0", "x", "33330200"),
    (".54.0", "i", "1"),
]
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "./output"))

NS = {"pls": "http://www.hp.com/schemas/imaging/pls/dev/1.0"}


def log(msg):
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


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


def ensure_destination_registered():
    existing = list_destinations()
    if not existing:
        # L'imprimante vient probablement de redemarrer (destinations effacees) : le
        # premier DestinationID attribue sera 0 (voir note SACRIFICE_DISPLAY plus haut).
        add_destination(SACRIFICE_DISPLAY)
        existing = list_destinations()
    if (HOST_ID, DEST_DISPLAY) in existing:
        log(f"Destination deja enregistree : {HOST_ID}^{DEST_DISPLAY}")
        return
    add_destination(DEST_DISPLAY)


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


def snmp_set(oid_suffix: str, type_char: str, value: str):
    oid = f"{SNMP_OID_BASE}{oid_suffix}"
    subprocess.run(
        ["snmpset", "-v1", "-c", SNMP_COMMUNITY, PRINTER_IP, oid, type_char, value],
        check=True,
        capture_output=True,
        timeout=10,
    )


def trigger_scan_via_snmp():
    """Reproduit la sequence SNMP capturee depuis un vrai client HP : configure les
    parametres du scan puis positionne l'OID d'etat a 2 pour demarrer physiquement le
    scan (voir PROTOCOL.md section 3bis)."""
    for suffix, type_char, value in SNMP_SCAN_PARAMS:
        snmp_set(suffix, type_char, value)
    snmp_set(".12.0", "i", "2")
    log("commande SNMP GO envoyee")


def reset_snmp_state():
    try:
        snmp_set(".12.0", "i", "1")
    except Exception as e:
        log(f"reset_snmp_state() erreur (ignoree) : {e!r}")


def fetch_scan_jpeg():
    """Le scan physique (mecanique) prend jusqu'a ~20s avant que l'imprimante commence a
    envoyer des donnees sur ce port : il faut attendre patiemment le premier octet, puis
    seulement ensuite basculer sur un timeout court pour detecter la fin du flux."""
    with socket.create_connection((PRINTER_IP, SCAN_PORT), timeout=15) as s:
        chunks = []
        s.settimeout(45)
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                s.settimeout(5)  # une fois le flux demarre, 5s d'inactivite = fin
        except socket.timeout:
            pass
        data = b"".join(chunks)

    idx = data.find(b"\xff\xd8\xff\xe0")
    if idx < 0:
        raise ValueError(f"marqueur JPEG SOI introuvable ({len(data)} octets recus)")
    log(f"en-tete proprietaire avant JPEG : {idx} octets")
    return data[idx:]


def save_as_pdf(jpeg_bytes: bytes):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("scan_%Y-%m-%d %H.%M.%S.pdf")
    dest = OUTPUT_DIR / name
    img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    img.save(dest, "PDF")
    log(f"scan sauvegarde : {dest} ({len(jpeg_bytes)} octets JPEG)")


def main():
    log(f"demarrage, cible {PRINTER_IP}, destination {HOST_ID}^{DEST_DISPLAY}")
    ensure_destination_registered()
    discover()
    log("sequence de decouverte effectuee")
    cycle = 0
    was_triggered = False  # front montant seulement : eviter de re-traiter le meme job
    while True:
        try:
            if cycle % 12 == 0:  # ~ toutes les 60s avec POLL_INTERVAL_S=5
                discover()
                ensure_destination_registered()  # au cas ou l'imprimante aurait redemarre
                # (efface ses destinations) pendant que ce pont continue de tourner
            n = poll_notifications()
            triggered = bool(
                n and n["ScanToHostID"] == HOST_ID and n["ScanToDeviceDisplay"] == DEST_DISPLAY
            )
            is_new_trigger = triggered and not was_triggered
            was_triggered = triggered
            if is_new_trigger:
                log(f"scan detecte pour {DEST_DISPLAY!r}, declenchement SNMP...")
                trigger_scan_via_snmp()
                try:
                    jpeg_bytes = fetch_scan_jpeg()
                    save_as_pdf(jpeg_bytes)
                finally:
                    reset_snmp_state()
        except Exception as e:
            log(f"erreur (ignoree, on continue) : {e!r}")
        cycle += 1
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
