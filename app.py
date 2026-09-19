#!/usr/bin/env python3
"""
Micro — a very small, self-hosted microblog.
One feed. Text + optional image. Posting happens from the terminal (see post.py).
"""
import html
import os
import sqlite3
import secrets
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

from flask import Flask, g, render_template, request, jsonify, send_from_directory, abort, Response, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "posts.db"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_CONTENT_LENGTH = 15 * 1024 * 1024  # 15 MB

# Uploaded images are shrunk to this width (the feed column is only
# 42rem/~672px wide, which comfortably covers retina displays too) -
# otherwise every first-time visitor would end up downloading full-size
# camera originals for no visual benefit.
UPLOAD_MAX_WIDTH = 1400
UPLOAD_JPEG_QUALITY = 85


def shrink_image_if_needed(path: Path) -> None:
    """Shrinks an image in place if it's wider than UPLOAD_MAX_WIDTH.
    GIFs are skipped (resizing would break the animation)."""
    if path.suffix.lower() == ".gif":
        return
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)  # fix phone-photo orientation
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
        print(f"[micro] Image shrink failed for {path.name}: {exc}")


# The token is generated on first start and stored in token.txt,
# or can be provided via the MICROBLOG_TOKEN environment variable.
TOKEN_FILE = BASE_DIR / "token.txt"


def get_token() -> str:
    env_token = os.environ.get("MICROBLOG_TOKEN")
    if env_token:
        return env_token
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token)
    print(f"[micro] New auth token generated and saved to {TOKEN_FILE}.")
    return token


TOKEN = get_token()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
# Behind nginx: makes url_for(..., _external=True) and request.url_root
# produce https:// and the real hostname instead of http://127.0.0.1
# (needed for correct links in the RSS feed).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


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
    # Migration for existing databases without the edited_at column.
    # try/except instead of a PRAGMA check, since gunicorn starts multiple
    # workers in parallel and a read-then-write race could otherwise crash
    # a worker on first boot.
    try:
        db.execute("ALTER TABLE posts ADD COLUMN edited_at TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e):
            raise
    db.commit()
    db.close()


# Schema is ensured at import time, not only under "python3 app.py" -
# otherwise gunicorn (production) would never create the table/migration.
init_db()


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def check_auth() -> bool:
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {TOKEN}"


def fmt_dt(iso_str: str | None) -> str | None:
    """ISO-UTC string -> local display 'DD.MM.YYYY HH:MM'."""
    if not iso_str:
        return None
    dt = datetime.fromisoformat(iso_str)
    return dt.astimezone().strftime("%d.%m.%Y %H:%M")


def row_to_dict(row: sqlite3.Row) -> dict:
    # The externally visible "id" is the gapless display number (num), not
    # the internal, never-reused SQLite row id.
    return {
        "id": row["num"],
        "text": row["text"],
        "image": row["image"],
        "created_at": row["created_at"],
        "created_at_display": fmt_dt(row["created_at"]),
        "edited": bool(row["edited_at"]),
        "edited_at_display": fmt_dt(row["edited_at"]),
    }


# Display number = position in creation order (oldest post = 1). Computed on
# every query instead of stored, so it stays gapless and shifts down
# automatically whenever an earlier post is deleted.
NUMBERED_POSTS_SQL = """
    SELECT id, text, image, created_at, edited_at,
           ROW_NUMBER() OVER (ORDER BY id ASC) AS num
    FROM posts
"""


def fetch_posts(limit: int | None = None) -> list[sqlite3.Row]:
    db = get_db()
    query = f"SELECT * FROM ({NUMBERED_POSTS_SQL}) ORDER BY num DESC"
    if limit is not None:
        query += " LIMIT ?"
        return db.execute(query, (limit,)).fetchall()
    return db.execute(query).fetchall()


def resolve_real_id(num: int) -> int | None:
    """Display number -> internal SQLite row id, for edit/delete."""
    db = get_db()
    row = db.execute(
        f"SELECT id FROM ({NUMBERED_POSTS_SQL}) WHERE num = ?", (num,)
    ).fetchone()
    return row["id"] if row else None


@app.route("/")
def timeline():
    posts = [row_to_dict(r) for r in fetch_posts()]
    return render_template("index.html", posts=posts)


@app.route("/post/<int:post_id>")
def single_post(post_id):
    real_id = resolve_real_id(post_id)
    if real_id is None:
        abort(404)
    db = get_db()
    row = db.execute(
        f"SELECT * FROM ({NUMBERED_POSTS_SQL}) WHERE num = ?", (post_id,)
    ).fetchone()
    return render_template("post.html", post=row_to_dict(row))


@app.route("/static/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


FEED_MAX_ITEMS = 30


def xmlesc(s: str | None) -> str:
    return html.escape(s or "", quote=True)


@app.route("/feed.xml")
def feed():
    posts = [row_to_dict(r) for r in fetch_posts(limit=FEED_MAX_ITEMS)]
    site_url = request.url_root.rstrip("/")

    items_xml = []
    for post in posts:
        permalink = url_for("single_post", post_id=post["id"], _external=True)
        if post["text"]:
            title = post["text"][:70] + ("…" if len(post["text"]) > 70 else "")
        else:
            title = f"Image post #{post['id']}"

        description_parts = []
        if post["text"]:
            description_parts.append(f"<p>{xmlesc(post['text'])}</p>")
        enclosure = ""
        if post["image"]:
            img_url = url_for("uploaded_file", filename=post["image"], _external=True)
            description_parts.append(f'<img src="{xmlesc(img_url)}" alt="">')
            ext = post["image"].rsplit(".", 1)[-1].lower()
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
            enclosure = f'<enclosure url="{xmlesc(img_url)}" type="{mime}" />'

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
    <description>Micro-blog feed</description>
    <language>en</language>
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
    if not text and "image" not in request.files:
        return jsonify({"error": "text or image required"}), 400

    image_name = None
    if "image" in request.files:
        file = request.files["image"]
        if file and file.filename and allowed_file(file.filename):
            ext = file.filename.rsplit(".", 1)[1].lower()
            image_name = f"{secrets.token_hex(8)}.{ext}"
            file.save(UPLOAD_DIR / image_name)
            shrink_image_if_needed(UPLOAD_DIR / image_name)

    created_at = datetime.now(timezone.utc).isoformat()
    db = get_db()
    db.execute(
        "INSERT INTO posts (text, image, created_at) VALUES (?, ?, ?)",
        (text, image_name, created_at),
    )
    db.commit()
    # The row just inserted has the highest internal id, so it also has the
    # highest display number = current total post count.
    num = db.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"]
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
    return jsonify([row_to_dict(r) for r in fetch_posts(limit=limit)]), 200


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8420, debug=False)
