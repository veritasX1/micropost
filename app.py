#!/usr/bin/env python3
"""
Micro — ein sehr kleiner, selbst gehosteter Microblog.
Ein Feed. Text + optionales Bild. Posten per Terminal (siehe post.py).
"""
import html
import os
import re
import shutil
import sqlite3
import secrets
import subprocess
from collections import OrderedDict
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

from flask import Flask, g, render_template, request, jsonify, send_from_directory, abort, Response, url_for
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "posts.db"
# static/uploads ist ein Symlink auf eine externe Platte (nicht die SD-Karte,
# auf der das System laeuft) - siehe README fuer den Grund. mkdir hier ruehrt
# den Symlink nicht an (exist_ok=True greift schon beim Symlink selbst).
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_VIDEO_EXT = {"mp4", "webm", "mov", "m4v"}
# Bewusst kein Video-Reencoding (kostet CPU auf dem Pi) - wer postet,
# komprimiert das Video vorher selbst. Nur ein Poster-Frame wird erzeugt.
MAX_CONTENT_LENGTH = 200 * 1024 * 1024  # 200 MB (mehrere Bilder + evtl. ein Video)

# Hochgeladene Bilder werden auf diese Breite verkleinert (Feed-Spalte ist
# nur 42rem/~672px breit, das deckt auch Retina-Displays gut ab) - sonst
# laedt jeder Erstbesucher unnoetig grosse Kamera-Originale mit.
UPLOAD_MAX_WIDTH = 1400
UPLOAD_JPEG_QUALITY = 85


def shrink_image_if_needed(path: Path) -> None:
    """Verkleinert ein Bild in-place, falls es breiter als UPLOAD_MAX_WIDTH
    ist. GIFs werden übersprungen (Animation ginge sonst verloren)."""
    if path.suffix.lower() == ".gif":
        return
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)  # Handy-Fotos korrekt drehen
            if img.width <= UPLOAD_MAX_WIDTH:
                return
            ratio = UPLOAD_MAX_WIDTH / img.width
            img = img.resize((UPLOAD_MAX_WIDTH, int(img.height * ratio)))
            if path.suffix.lower() in (".jpg", ".jpeg"):
                img = img.convert("RGB")
                img.save(path, "JPEG", quality=UPLOAD_JPEG_QUALITY, optimize=True)
            else:
                img.save(path, optimize=True)
    except Exception as exc:
        print(f"[micro] Bild-Verkleinerung fehlgeschlagen fuer {path.name}: {exc}", flush=True)


def extract_video_poster(video_path: Path, poster_path: Path) -> bool:
    """Ein Standbild als Poster fuers <video>-Element, damit beim Laden der
    Seite kein Frame des Videos selbst geladen werden muss (das Video laedt
    dank preload="none" ohnehin erst, wenn wirklich auf Play gedrueckt wird).
    Kein Reencoding des Videos selbst - siehe MAX_CONTENT_LENGTH-Kommentar."""
    for ss in ("00:00:00.5", "00:00:00"):
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(video_path), "-ss", ss, "-vframes", "1",
                 "-vf", "scale='min(960,iw)':-2", str(poster_path)],
                check=True, capture_output=True, timeout=30,
            )
            if poster_path.exists():
                return True
        except Exception as exc:
            print(f"[micro] Poster-Extraktion (ss={ss}) fehlgeschlagen fuer {video_path.name}: {exc}", flush=True)
    return False


# Token wird beim ersten Start generiert und in token.txt abgelegt,
# oder per Umgebungsvariable MICROBLOG_TOKEN vorgegeben.
TOKEN_FILE = BASE_DIR / "token.txt"


def get_token() -> str:
    env_token = os.environ.get("MICROBLOG_TOKEN")
    if env_token:
        return env_token
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token)
    print(f"[micro] Neues Auth-Token erzeugt und in {TOKEN_FILE} gespeichert.")
    return token


TOKEN = get_token()

# Ueberschrift ohne KI: einfache Textheuristik statt eines Modells, damit
# das keine zusaetzliche RAM/CPU-Last verursacht. Erste Zeile des Texts, an
# einer Wortgrenze auf HEADLINE_MAX_LEN Zeichen gekuerzt, damit sie nie
# umbricht. Wird live berechnet statt gespeichert, bleibt also auch nach
# einem Edit automatisch aktuell.
HEADLINE_MAX_LEN = 60


def compute_headline(text: str, media: list[dict] | None) -> str:
    text = (text or "").strip()
    if text:
        first_line = text.splitlines()[0].strip()
        if len(first_line) > HEADLINE_MAX_LEN:
            truncated = first_line[:HEADLINE_MAX_LEN]
            if " " in truncated:
                truncated = truncated.rsplit(" ", 1)[0]
            first_line = truncated.rstrip() + "…"
        return first_line
    if any(m["kind"] == "video" for m in media or []):
        return "Video post"
    if media:
        return "Image post"
    return ""


# *word* -> highlighted in red, a nod to the black/red two-color ribbon on
# old typewriters. (?<!\s)/(?!\s) keeps whitespace right after/before an
# asterisk from counting, which also rules out most "2 * 3" math. Escaping
# happens first, the rest is plain markup - no post text can inject its own
# HTML this way.
EMPHASIS_RE = re.compile(r"\*(?!\s)([^*\n]+?)(?<!\s)\*")


def render_post_text(text: str) -> Markup:
    escaped = str(escape(text or ""))
    highlighted = EMPHASIS_RE.sub(r'<span class="emph">\1</span>', escaped)
    return Markup(highlighted)


# Optionale Textsicherung auf einem zweiten Medium (urspruenglich fuer ein
# echtes USB-Diskettenlaufwerk gebaut, funktioniert aber mit jedem
# beschreibbaren Pfad - eine externe Platte, ein USB-Stick, ein Netzlaufwerk).
# Aus, solange MICRO_FLOPPY_DIR nicht gesetzt ist - das ist ein
# Nice-to-have fuer ein Zweitbackup, kein Kernfeature, und soll nicht
# ungefragt irgendwo im Dateisystem herumschreiben.
FLOPPY_DIR = Path(os.environ["MICRO_FLOPPY_DIR"]).expanduser() if os.environ.get("MICRO_FLOPPY_DIR") else None


def human_size(num_bytes: float) -> str:
    """z.B. 119 -> '119B', 1234 -> '1.2K', 1500000 -> '1.4M' - angelehnt an
    die Ausgabe von `df -h` fuer die Fuellstandsanzeige im Header."""
    size = float(num_bytes)
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            if unit == "B":
                return f"{size:.0f}{unit}"
            return f"{size:.1f}{unit}" if size < 100 else f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.0f}G"


def get_floppy_usage() -> dict | None:
    """Belegung des Backup-Mediums fuer die Fuellstandsanzeige im Header.
    None wenn das Feature aus ist oder der Pfad gerade nicht erreichbar ist
    (z.B. Wechseldatentraeger nicht eingelegt) - die Anzeige blendet sich
    dann im Template einfach aus."""
    if FLOPPY_DIR is None:
        return None
    try:
        usage = shutil.disk_usage(FLOPPY_DIR)
    except OSError:
        return None
    if usage.total == 0:
        return None
    return {
        "percent": round(usage.used / usage.total * 100),
        "used_display": human_size(usage.used),
        "total_display": human_size(usage.total),
    }


def save_text_to_floppy(post_id: int, text: str, created_at_iso: str, media: list[dict] | None = None) -> bool:
    """Dateiname: <Datum>_<vierstellige Postnummer>.txt, z.B. 20260919_0020.txt -
    dadurch sowohl chronologisch als auch alphabetisch gleich sortiert, und
    auf einen Blick klar, welcher Post/Tag gemeint ist, ohne im Blog
    nachschlagen zu muessen. Gibt zurueck, ob es wirklich geklappt hat (z.B.
    False wenn das Medium voll oder nicht erreichbar ist), damit der Erfolg
    pro Post in der DB vermerkt werden kann. No-op (False), wenn das
    Feature per MICRO_FLOPPY_DIR gar nicht aktiviert ist."""
    if FLOPPY_DIR is None:
        return False
    headline = compute_headline(text, media)
    body = text.strip() if text else "Post contains a media file only, no additional text."
    content = f"Post #{post_id} {headline}\n{'-' * 40}\n{body}"
    try:
        date_prefix = datetime.fromisoformat(created_at_iso).strftime("%Y%m%d")
        filename = f"{date_prefix}_{post_id:04d}.txt"
        FLOPPY_DIR.mkdir(parents=True, exist_ok=True)
        (FLOPPY_DIR / filename).write_text(content, encoding="utf-8")
        return True
    except OSError as exc:
        print(f"[micro] Textsicherung fehlgeschlagen fuer #{post_id}: {exc}", flush=True)
        return False


def backfill_pending_floppy_saves(db) -> None:
    """Versucht erneut, alle Posts auf das Backup-Medium zu schreiben, die
    noch nicht erfolgreich gesichert sind - entweder weil sie aelter als
    dieses Feature sind (floppy_saved war noch NULL) oder weil ein
    frueherer Versuch fehlgeschlagen ist (z.B. Medium damals voll/nicht
    eingelegt). Laeuft bei jedem neuen Post mit, damit ein frisch
    eingelegtes Medium den Rueckstand automatisch aufholt."""
    if FLOPPY_DIR is None:
        return
    pending = db.execute(
        "SELECT id, text, created_at FROM posts "
        "WHERE floppy_saved IS NULL OR floppy_saved = 0"
    ).fetchall()
    if not pending:
        return
    num_by_id = {
        r["id"]: r["num"]
        for r in db.execute(f"SELECT id, num FROM ({NUMBERED_POSTS_SQL})").fetchall()
    }
    media_map = get_media_map(db, [row["id"] for row in pending])
    for row in pending:
        num = num_by_id.get(row["id"])
        if num is None:
            continue
        ok = save_text_to_floppy(num, row["text"], row["created_at"], media_map.get(row["id"]))
        db.execute("UPDATE posts SET floppy_saved = ? WHERE id = ?", (int(ok), row["id"]))
    db.commit()


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
# Hinter nginx: sorgt dafuer, dass url_for(..., _external=True) und
# request.url_root https:// und den echten Hostnamen liefern statt
# faelschlich http://127.0.0.1 (wichtig fuer den RSS-Feed).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


@app.context_processor
def inject_floppy_usage():
    # In jedem Template ohne extra Zutun als `floppy` verfuegbar. Liefert
    # None (Anzeige blendet sich dann aus), solange MICRO_FLOPPY_DIR nicht
    # gesetzt ist - das Feature ist also standardmaessig unsichtbar.
    return {"floppy": get_floppy_usage()}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            image TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    # Migration fuer bestehende Datenbanken ohne edited_at-Spalte. try/except
    # statt PRAGMA-Check, da gunicorn mehrere Worker parallel startet und ein
    # Read-then-write-Race sonst zum Absturz eines Workers fuehren kann.
    try:
        db.execute("ALTER TABLE posts ADD COLUMN edited_at TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e):
            raise
    try:
        db.execute("ALTER TABLE posts ADD COLUMN floppy_saved INTEGER")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e):
            raise
    # Mehrere Bilder bzw. ein Video pro Post. Die alte "image"-Spalte in
    # posts bleibt fuer historische Posts (nie migriert) - row_to_dict()
    # baut daraus bei Bedarf eine Ein-Bild-media-Liste nach.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS post_media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            filename TEXT NOT NULL,
            poster TEXT,
            position INTEGER NOT NULL,
            FOREIGN KEY (post_id) REFERENCES posts(id)
        )
        """
    )
    db.commit()
    db.close()


# Schema wird beim Import sichergestellt, nicht nur bei "python3 app.py" -
# sonst legt gunicorn (Produktivbetrieb) die Tabelle/Migration nie an.
init_db()


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def allowed_video_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_VIDEO_EXT


def check_auth() -> bool:
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {TOKEN}"


def fmt_dt(iso_str: str | None) -> str | None:
    """ISO-UTC-String -> lokale Anzeige 'DD.MM.YYYY HH:MM'"""
    if not iso_str:
        return None
    dt = datetime.fromisoformat(iso_str)
    return dt.astimezone().strftime("%d.%m.%Y %H:%M")


def get_media_map(db, real_ids: list[int]) -> dict[int, list[dict]]:
    """post_id -> [{"kind","filename","poster"}, ...] in Post-Reihenfolge,
    fuer eine Reihe von Posts in einer Abfrage statt N+1."""
    if not real_ids:
        return {}
    placeholders = ",".join("?" for _ in real_ids)
    rows = db.execute(
        f"SELECT post_id, kind, filename, poster FROM post_media "
        f"WHERE post_id IN ({placeholders}) ORDER BY post_id, position",
        real_ids,
    ).fetchall()
    media_map: dict[int, list[dict]] = {}
    for r in rows:
        media_map.setdefault(r["post_id"], []).append(
            {"kind": r["kind"], "filename": r["filename"], "poster": r["poster"]}
        )
    return media_map


def row_to_dict(row: sqlite3.Row, media: list[dict] | None = None) -> dict:
    # "id" nach aussen ist die fortlaufende Anzeige-Nummer (num), nicht die
    # interne, nie wiederverwendete SQLite-Zeilen-ID.
    if media is None:
        # Alte Posts vor post_media: aus der Legacy-image-Spalte nachbauen.
        media = [{"kind": "image", "filename": row["image"], "poster": None}] if row["image"] else []
    return {
        "id": row["num"],
        "text": row["text"],
        "text_html": render_post_text(row["text"]),
        "headline": compute_headline(row["text"], media),
        "media": media,
        "created_at": row["created_at"],
        "created_at_display": fmt_dt(row["created_at"]),
        "edited": bool(row["edited_at"]),
        "edited_at_display": fmt_dt(row["edited_at"]),
        # None = nicht zutreffend (kein Text oder aelter als dieses Feature),
        # True/False = Diskettensicherung tatsaechlich erfolgreich/fehlgeschlagen.
        "floppy_saved": None if row["floppy_saved"] is None else bool(row["floppy_saved"]),
    }


# Fortlaufende Nummer = Position in Erstellungsreihenfolge (aeltester Post = 1).
# Wird bei jeder Abfrage neu berechnet statt gespeichert, damit sie beim
# Loeschen eines Posts luecken-frei nachrueckt.
NUMBERED_POSTS_SQL = """
    SELECT id, text, image, created_at, edited_at, floppy_saved,
           ROW_NUMBER() OVER (ORDER BY id ASC) AS num
    FROM posts
"""


def fetch_posts(limit: int | None = None, offset: int = 0) -> list[sqlite3.Row]:
    db = get_db()
    query = f"SELECT * FROM ({NUMBERED_POSTS_SQL}) ORDER BY num DESC"
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        return db.execute(query, (limit, offset)).fetchall()
    return db.execute(query).fetchall()


def build_archive_index() -> "OrderedDict[str, OrderedDict[str, list]]":
    """Jahr -> Datum -> [(Postnummer, Uhrzeit, Ueberschrift), ...], neueste
    zuerst - fuer die Seitenleiste. Nur die paar Metadaten, nicht der volle
    Text/Bild (die Ueberschrift wird aus Text + Medienart live berechnet,
    siehe compute_headline)."""
    db = get_db()
    rows = db.execute(f"SELECT num, created_at, text, id FROM ({NUMBERED_POSTS_SQL}) ORDER BY num DESC").fetchall()
    media_map = get_media_map(db, [row["id"] for row in rows])
    years: "OrderedDict[str, OrderedDict[str, list]]" = OrderedDict()
    for row in rows:
        dt = datetime.fromisoformat(row["created_at"]).astimezone()
        year = dt.strftime("%Y")
        date_label = dt.strftime("%d.%m.%Y")
        headline = compute_headline(row["text"], media_map.get(row["id"]))
        years.setdefault(year, OrderedDict())
        years[year].setdefault(date_label, []).append((row["num"], dt.strftime("%H:%M"), headline))
    return years


def resolve_real_id(num: int) -> int | None:
    """Anzeige-Nummer -> interne SQLite-Zeilen-ID, fuer edit/delete."""
    db = get_db()
    row = db.execute(
        f"SELECT id FROM ({NUMBERED_POSTS_SQL}) WHERE num = ?", (num,)
    ).fetchone()
    return row["id"] if row else None


POSTS_PER_PAGE = 10


@app.route("/")
@app.route("/page/<int:page>")
def timeline(page: int = 1):
    if page < 1:
        abort(404)
    db = get_db()
    total = db.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"]
    total_pages = max(1, (total + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE)
    if page > total_pages and total > 0:
        abort(404)
    offset = (page - 1) * POSTS_PER_PAGE
    rows = fetch_posts(limit=POSTS_PER_PAGE, offset=offset)
    media_map = get_media_map(db, [r["id"] for r in rows])
    posts = [row_to_dict(r, media_map.get(r["id"])) for r in rows]
    return render_template(
        "index.html", posts=posts, page=page, total_pages=total_pages,
        archive=build_archive_index(),
    )


@app.route("/post/<int:post_id>")
def single_post(post_id):
    real_id = resolve_real_id(post_id)
    if real_id is None:
        abort(404)
    db = get_db()
    row = db.execute(
        f"SELECT * FROM ({NUMBERED_POSTS_SQL}) WHERE num = ?", (post_id,)
    ).fetchone()
    media_map = get_media_map(db, [row["id"]])
    return render_template(
        "post.html", post=row_to_dict(row, media_map.get(row["id"])), archive=build_archive_index()
    )


@app.route("/static/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


FEED_MAX_ITEMS = 30


def xmlesc(s: str | None) -> str:
    return html.escape(s or "", quote=True)


@app.route("/feed.xml")
def feed():
    db = get_db()
    rows = fetch_posts(limit=FEED_MAX_ITEMS)
    media_map = get_media_map(db, [r["id"] for r in rows])
    posts = [row_to_dict(r, media_map.get(r["id"])) for r in rows]
    site_url = request.url_root.rstrip("/")

    items_xml = []
    for post in posts:
        permalink = url_for("single_post", post_id=post["id"], _external=True)
        if post["text"]:
            title = post["text"][:70] + ("…" if len(post["text"]) > 70 else "")
        elif post["media"] and post["media"][0]["kind"] == "video":
            title = f"Video-Post #{post['id']}"
        else:
            title = f"Bild-Post #{post['id']}"

        description_parts = []
        if post["text"]:
            description_parts.append(f"<p>{xmlesc(post['text'])}</p>")
        enclosure = ""
        # RSS <enclosure> erlaubt nur eine Datei - bei mehreren Bildern
        # zaehlt nur das erste als Enclosure, die anderen nur als <img> im
        # Beschreibungstext. Video zaehlt als Enclosure statt Bild.
        media = post["media"]
        if media:
            first = media[0]
            if first["kind"] == "video":
                video_url = url_for("uploaded_file", filename=first["filename"], _external=True)
                ext = first["filename"].rsplit(".", 1)[-1].lower()
                mime = {"mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
                        "m4v": "video/x-m4v"}.get(ext, "video/mp4")
                if first["poster"]:
                    poster_url = url_for("uploaded_file", filename=first["poster"], _external=True)
                    description_parts.append(f'<img src="{xmlesc(poster_url)}" alt="">')
                enclosure = f'<enclosure url="{xmlesc(video_url)}" type="{mime}" />'
            else:
                for m in media:
                    img_url = url_for("uploaded_file", filename=m["filename"], _external=True)
                    description_parts.append(f'<img src="{xmlesc(img_url)}" alt="">')
                ext = first["filename"].rsplit(".", 1)[-1].lower()
                mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                        "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
                first_url = url_for("uploaded_file", filename=first["filename"], _external=True)
                enclosure = f'<enclosure url="{xmlesc(first_url)}" type="{mime}" />'

        pub_dt = datetime.fromisoformat(post["created_at"])
        items_xml.append(f"""
    <item>
      <title>{xmlesc(title)}</title>
      <link>{xmlesc(permalink)}</link>
      <guid isPermaLink="true">{xmlesc(permalink)}</guid>
      <pubDate>{format_datetime(pub_dt)}</pubDate>
      {enclosure}
      <description><![CDATA[{''.join(description_parts)}]]></description>
    </item>""")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>micro</title>
    <link>{xmlesc(site_url)}</link>
    <description>Micro-Blog Feed</description>
    <language>de-de</language>
    {''.join(items_xml)}
  </channel>
</rss>
"""
    return Response(xml, mimetype="application/rss+xml")


@app.route("/api/post", methods=["POST"])
def api_post():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401

    text = (request.form.get("text") or "").strip()
    # "image[]" zusaetzlich zu "image" akzeptieren - manche Clients (z.B.
    # manche Multi-File-Modi von Android-HTTP-Shortcuts-Apps) haengen bei
    # mehreren Dateien im selben Feld ein "[]" an den Feldnamen an.
    images = [
        f for f in request.files.getlist("image") + request.files.getlist("image[]")
        if f and f.filename
    ]
    video = request.files.get("video") or request.files.get("video[]")
    has_video = bool(video and video.filename)
    if not text and not images and not has_video:
        if request.files:
            # Hilft beim Debuggen von Client-Konfigurationen, die einen
            # unerwarteten Feldnamen schicken - siehe --error-logfile.
            print(f"[micro] Post ohne text/image/video, aber Dateifelder vorhanden: {list(request.files.keys())}", flush=True)
        return jsonify({"error": "text, image or video required"}), 400
    if video and has_video and not allowed_video_file(video.filename):
        return jsonify({"error": "video type not allowed"}), 400
    for f in images:
        if not allowed_file(f.filename):
            return jsonify({"error": f"image type not allowed: {f.filename}"}), 400

    created_at = datetime.now(timezone.utc).isoformat()
    db = get_db()
    backfill_pending_floppy_saves(db)
    # image-Spalte bleibt fuer neue Posts leer - Medien landen ausschliesslich
    # in post_media, dafuer wird die Post-id (real_id) vorher gebraucht.
    cur = db.execute(
        "INSERT INTO posts (text, image, created_at) VALUES (?, NULL, ?)",
        (text, created_at),
    )
    real_id = cur.lastrowid

    for position, f in enumerate(images):
        ext = f.filename.rsplit(".", 1)[1].lower()
        name = f"{secrets.token_hex(8)}.{ext}"
        f.save(UPLOAD_DIR / name)
        shrink_image_if_needed(UPLOAD_DIR / name)
        db.execute(
            "INSERT INTO post_media (post_id, kind, filename, position) VALUES (?, 'image', ?, ?)",
            (real_id, name, position),
        )
    if has_video:
        ext = video.filename.rsplit(".", 1)[1].lower()
        vname = f"{secrets.token_hex(8)}.{ext}"
        video.save(UPLOAD_DIR / vname)
        poster_name = f"{secrets.token_hex(8)}.jpg"
        poster_ok = extract_video_poster(UPLOAD_DIR / vname, UPLOAD_DIR / poster_name)
        db.execute(
            "INSERT INTO post_media (post_id, kind, filename, poster, position) VALUES (?, 'video', ?, ?, ?)",
            (real_id, vname, poster_name if poster_ok else None, len(images)),
        )
    db.commit()
    # Der eben eingefuegte Post hat die hoechste interne ID, also auch die
    # hoechste Anzeige-Nummer = aktuelle Gesamtzahl der Posts.
    num = db.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"]
    media_for_floppy = [{"kind": "image"} for _ in images] + (
        [{"kind": "video"}] if has_video else []
    )
    floppy_ok = save_text_to_floppy(num, text, created_at, media_for_floppy)
    db.execute("UPDATE posts SET floppy_saved = ? WHERE id = ?", (int(floppy_ok), real_id))
    db.commit()
    return jsonify(
        {"status": "ok", "id": num, "created_at_display": fmt_dt(created_at)}
    ), 201


@app.route("/api/edit/<int:post_id>", methods=["POST"])
def api_edit(post_id):
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401

    text = (request.form.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400

    db = get_db()
    real_id = resolve_real_id(post_id)
    if real_id is None:
        return jsonify({"error": "not found"}), 404

    edited_at = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE posts SET text = ?, edited_at = ? WHERE id = ?",
        (text, edited_at, real_id),
    )
    db.commit()
    return jsonify(
        {"status": "edited", "id": post_id, "edited_at_display": fmt_dt(edited_at)}
    ), 200


@app.route("/api/delete/<int:post_id>", methods=["POST"])
def api_delete(post_id):
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401
    db = get_db()
    real_id = resolve_real_id(post_id)
    if real_id is None:
        return jsonify({"error": "not found"}), 404
    row = db.execute("SELECT image FROM posts WHERE id = ?", (real_id,)).fetchone()
    if row["image"]:
        img_path = UPLOAD_DIR / row["image"]
        if img_path.exists():
            img_path.unlink()
    media_rows = db.execute(
        "SELECT filename, poster FROM post_media WHERE post_id = ?", (real_id,)
    ).fetchall()
    for m in media_rows:
        for name in (m["filename"], m["poster"]):
            if name:
                path = UPLOAD_DIR / name
                if path.exists():
                    path.unlink()
    db.execute("DELETE FROM post_media WHERE post_id = ?", (real_id,))
    db.execute("DELETE FROM posts WHERE id = ?", (real_id,))
    db.commit()
    return jsonify({"status": "deleted"}), 200


@app.route("/api/posts", methods=["GET"])
def api_posts():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401
    try:
        limit = int(request.args.get("limit", 10))
    except ValueError:
        limit = 10
    limit = max(1, min(limit, 100))
    db = get_db()
    rows = fetch_posts(limit=limit)
    media_map = get_media_map(db, [r["id"] for r in rows])
    return jsonify([row_to_dict(r, media_map.get(r["id"])) for r in rows]), 200


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8420, debug=False)
