#!/usr/bin/env python3
"""
post.py — vom Terminal aus auf den Microblog posten, bearbeiten, loeschen, auflisten.

Einrichtung (einmalig):
    export MICROBLOG_URL="https://micro.deine-domain.de"
    export MICROBLOG_TOKEN="<token aus token.txt auf dem Server>"

Nutzung (direkt):
    ./post.py "Nur Text, kein Bild."
    ./post.py "Text mit Bild" --image ~/Bilder/foto.jpg
    ./post.py "Galerie" --image a.jpg --image b.jpg --image c.jpg
    ./post.py "Kurzes Video" --video ~/Videos/clip.mp4
    ./post.py --show
    ./post.py --delete 13
    ./post.py --edit 24 "neuer text"

Komfortabler direkt ueber den micropost-Wrapper:
    micropost hallo schoene welt
    micropost show
    micropost del #13
    micropost edit #24 neuer text
"""
import argparse
import os
import sys

import requests


def main():
    parser = argparse.ArgumentParser(description="Auf den Microblog posten/verwalten.")
    parser.add_argument("text", nargs="?", default="", help="Der Post-Text (Standardaktion: posten)")
    parser.add_argument("--image", "-i", action="append", help="Pfad zu einem Bild (mehrfach angebbar fuer eine Galerie)")
    parser.add_argument("--video", "-v", help="Pfad zu einem kurzen Video (wird nicht neu komprimiert)")
    parser.add_argument("--url", default=os.environ.get("MICROBLOG_URL"), help="Server-URL")
    parser.add_argument("--token", default=os.environ.get("MICROBLOG_TOKEN"), help="Auth-Token")
    parser.add_argument("--show", action="store_true", help="Letzte Posts auflisten")
    parser.add_argument("--limit", type=int, default=10, help="Anzahl bei --show (Standard 10)")
    parser.add_argument("--delete", metavar="ID", help="Post mit dieser ID loeschen")
    parser.add_argument("--edit", metavar="ID", help="Post mit dieser ID bearbeiten (neuer Text im 'text'-Argument)")
    args = parser.parse_args()

    if not args.url or not args.token:
        sys.exit(
            "Fehler: MICROBLOG_URL und MICROBLOG_TOKEN muessen gesetzt sein "
            "(als Umgebungsvariable oder --url/--token)."
        )

    headers = {"Authorization": f"Bearer {args.token}"}
    base = args.url.rstrip("/")

    if args.show:
        resp = requests.get(f"{base}/api/posts", headers=headers, params={"limit": args.limit}, timeout=30)
        if resp.status_code != 200:
            sys.exit(f"Fehler ({resp.status_code}): {resp.text}")
        posts = resp.json()
        if not posts:
            print("(keine Posts)")
            return
        for p in posts:
            marker = f"  (bearbeitet {p['edited_at_display']})" if p.get("edited") else ""
            media = p.get("media") or []
            if not media:
                media_note = ""
            elif len(media) == 1 and media[0]["kind"] == "video":
                media_note = "  [Video]"
            elif len(media) == 1:
                media_note = f"  [Bild: {media[0]['filename']}]"
            else:
                media_note = f"  [{len(media)} Bilder]"
            print(f"#{p['id']}  {p['created_at_display']}{marker}{media_note}")
            if p.get("text"):
                print(f"    {p['text']}")
        return

    if args.delete:
        post_id = args.delete.lstrip("#")
        resp = requests.post(f"{base}/api/delete/{post_id}", headers=headers, timeout=30)
        if resp.status_code == 200:
            print(f"#{post_id} geloescht.")
        else:
            sys.exit(f"Fehler ({resp.status_code}): {resp.text}")
        return

    if args.edit:
        post_id = args.edit.lstrip("#")
        if not args.text:
            sys.exit("Fehler: Bitte neuen Text angeben (micropost edit #N neuer text).")
        resp = requests.post(
            f"{base}/api/edit/{post_id}", headers=headers, data={"text": args.text}, timeout=30
        )
        if resp.status_code == 200:
            print(f"#{post_id} bearbeitet.")
        else:
            sys.exit(f"Fehler ({resp.status_code}): {resp.text}")
        return

    # Standardaktion: neuen Post erstellen
    text = args.text
    if not text and not args.image and not args.video:
        sys.exit("Fehler: Bitte Text und/oder --image/--video angeben.")

    data = {"text": text}
    files = []
    opened = []
    for path in args.image or []:
        if not os.path.isfile(path):
            sys.exit(f"Fehler: Datei nicht gefunden: {path}")
        f = open(path, "rb")
        opened.append(f)
        files.append(("image", f))
    if args.video:
        if not os.path.isfile(args.video):
            sys.exit(f"Fehler: Datei nicht gefunden: {args.video}")
        f = open(args.video, "rb")
        opened.append(f)
        files.append(("video", f))

    try:
        resp = requests.post(
            f"{base}/api/post", headers=headers, data=data, files=files or None, timeout=120
        )
    finally:
        for f in opened:
            f.close()

    if resp.status_code == 201:
        info = resp.json()
        print(f"gepostet als #{info.get('id')} ({info.get('created_at_display', '')}).")
    else:
        sys.exit(f"Fehler ({resp.status_code}): {resp.text}")


if __name__ == "__main__":
    main()
