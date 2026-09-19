# micro

A very small, self-hosted microblog. One feed. Text and/or an image per
post. No comments, no likes, no threads, no web-based posting UI on
purpose — you post from the terminal.

```
$ micropost just shipped a thing
posted as #7 (18.09.2026 09:12).

$ micropost ~/Pictures/trip.jpg what a day
posted as #8 (18.09.2026 09:13).

$ micropost show
#8  18.09.2026 09:13  [image: 9f3a1c2b8e6d4f01.jpg]
    what a day
#7  18.09.2026 09:12
    just shipped a thing
```

## Features

- Single chronological feed, plain-text monospace design, no JS
- Post text and/or an image (png/jpg/jpeg/gif/webp, up to 15 MB)
- Uploaded images are automatically shrunk to a sane width (default
  1400px, JPEG quality 85) and re-oriented from EXIF data — so a raw
  phone/camera photo never ships multiple megabytes to every reader
- Edit and delete posts from the CLI
- Posts get a gapless display number (`#1`, `#2`, ...) in creation order;
  deleting a post shifts all later numbers down by one, so there are never
  holes in the sequence
- Every post has its own permalink page (`/post/<id>`), and the whole feed
  is subscribable as RSS (`/feed.xml`, auto-discovered by feed readers)
- Token-based auth (a single, randomly generated bearer token — this is a
  personal single-user tool, not a multi-user platform)
- `micropost` shell wrapper: post without quoting your text, and drop an
  image path anywhere in the command to attach it automatically
- Post from your phone too — no app needed, just a couple of HTTP
  Shortcuts (see [Posting from your phone](#posting-from-your-phone-android))

## Architecture

- `app.py` — Flask app, SQLite storage, a handful of JSON endpoints
- `templates/index.html`, `static/style.css` — the (public, read-only) web
  feed
- `post.py` — CLI client, talks to the API over HTTPS
- `micropost` — a thin bash wrapper around `post.py` for a nicer CLI (no
  quoting, subcommands like `show`/`del`/`edit`, automatic image detection)

The web feed (`/`) is public and unauthenticated by design — it's meant to
be readable by anyone, like a blog. Everything that changes state
(`/api/post`, `/api/edit/<id>`, `/api/delete/<id>`, `/api/posts`) requires
the bearer token.

## Server setup

```bash
git clone https://github.com/<you>/micropost.git
cd micropost

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run once to create the database and generate an auth token
python3 app.py
# Ctrl+C once it's up - the token is now in token.txt
cat token.txt
```

### As a systemd service

1. Copy `micro.service.example` to `/etc/systemd/system/micro.service`
2. Adjust `User`, `WorkingDirectory` and the `ExecStart` path
3. Leave the token out of the unit file — `app.py` picks it up from
   `token.txt` in the working directory on its own. (You *can* pass it via
   an `Environment=MICROBLOG_TOKEN=...` line instead if you prefer, but
   don't leave a placeholder value in there — anyone who can read the unit
   file would then know your auth token.)

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now micro
sudo systemctl status micro
```

Runs on `127.0.0.1:8420` by default — put a reverse proxy in front of it
for real deployments (see below), don't expose it directly.

### Putting a reverse proxy in front (nginx example)

```nginx
server {
    listen 443 ssl;
    server_name micro.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/micro.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/micro.yourdomain.com/privkey.pem;

    # Match (or raise) the 15 MB limit in app.py, or image uploads will be
    # rejected by nginx before they ever reach the app.
    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:8420;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Get a certificate however you normally do (e.g. `certbot --nginx -d
micro.yourdomain.com`).

## Client setup (on your own machine)

```bash
# Put both files somewhere on your PATH, e.g. ~/bin
cp post.py ~/bin/post.py       # or rename it if you like, micropost finds
cp micropost ~/bin/micropost   # it next to itself either way
chmod +x ~/bin/post.py ~/bin/micropost
```

Add to your shell profile (`~/.bashrc`, `~/.zshrc`, ...):

```bash
export MICROBLOG_URL="https://micro.yourdomain.com"
export MICROBLOG_TOKEN="<token from token.txt on the server>"
```

Reload your shell (`source ~/.bashrc`) and you're set.

## Usage

```
micropost your text without quotes
    Post the text as-is. No quotes needed.

micropost /path/to/image.jpg optional text
    An image path anywhere among the words is detected automatically
    and attached (handy when you drag a file into the terminal).

micropost img /path/to/image.jpg [optional text]
    Same thing, explicitly.

micropost show
    List the 10 most recent posts.

micropost del N
    Delete post number N.

micropost edit N new text
    Replace the text of post number N.

micropost help
    Show all of the above.
```

**Note on `#`:** type IDs *without* the leading `#` (`micropost del 13`, not
`micropost del #13`). In bash, an unquoted `#` starts a comment and
everything after it on the line is silently discarded before your command
even runs — this is a shell quirk, not something the script can work
around.

You can also call `post.py` directly (same flags as documented in its
docstring) if you don't want the wrapper's conveniences.

## Posting from your phone (Android)

There's no app — instead, use the free, open-source **[HTTP
Shortcuts](https://play.google.com/store/apps/details?id=ch.rmy.android.http_shortcuts)**
app (by Roland Meyer) to build two small shortcuts that call the same API
`post.py` uses. Takes a few minutes, no coding involved.

### Shortcut 1: post an image (from the share sheet)

1. Install HTTP Shortcuts, create a new shortcut.
2. Method: `POST`, URL: `https://micro.yourdomain.com/api/post`
3. Header: `Authorization` → `Bearer <your token>`
4. Request body type: **Parameter list (form-data)** — this is the
   multipart option that supports file uploads; the plain-text and
   x-www-form-urlencoded options won't work here.
5. Add a parameter `text`, type *Text*. For its value, don't type
   anything directly — tap the variable-insert icon next to the field and
   create a new variable of type **Text input** (e.g. name it `caption`).
   This makes the app prompt you for a caption every time the shortcut
   runs.
6. Add a parameter `image`, type *Single file*. Pick "Open file picker" as
   its source — this is just the fallback for when you launch the
   shortcut directly; when triggered via the share sheet (next step) the
   shared image is used automatically instead.
7. In the shortcut's trigger/execution settings, enable **"Use as share
   target"** for image files (`image/*`).
8. Optional: under response handling, set it to show nothing or a toast
   instead of the full JSON response.

Now: share a photo from your gallery → pick this shortcut → type a
caption → posted.

### Shortcut 2: post text only (from the home screen)

Duplicate shortcut 1 (keeps the URL/header), then:

1. Remove the `image` parameter — keep only `text` (you can reuse the same
   `caption` variable, or create a separate one).
2. Disable "use as share target"; instead add the shortcut to your home
   screen.

Tap the icon, type your text, done — the mobile equivalent of `micropost
your text without quotes`.

## API

All endpoints below except the public ones (`/`, `/post/<id>`, `/feed.xml`,
`/static/...`) require `Authorization: Bearer <token>`.

| Method | Path                | Description                          |
|--------|---------------------|---------------------------------------|
| GET    | `/`                 | Public HTML feed                      |
| GET    | `/post/<id>`        | Public permalink page for one post    |
| GET    | `/feed.xml`         | Public RSS 2.0 feed (last 30 posts)   |
| GET    | `/api/posts`        | JSON list, `?limit=` (default 10)     |
| POST   | `/api/post`         | Create a post (`text`, optional `image` file) |
| POST   | `/api/edit/<id>`    | Replace a post's text (`text`)        |
| POST   | `/api/delete/<id>`  | Delete a post                         |

`<id>` is the gapless display number shown in the feed / `show` output, not
an internal database id.

## Security notes

- The single bearer token is the only access control — treat it like a
  password. It's generated with `secrets.token_urlsafe(32)`.
- Uploaded image filenames are replaced with random hex names; the
  extension is whitelisted.
- Run it behind TLS. The token goes over the wire on every write request.

## License

MIT — see [LICENSE](LICENSE).
