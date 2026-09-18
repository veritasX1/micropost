#!/usr/bin/env python3
"""
post.py — post, edit, delete and list entries from the terminal.

One-time setup:
    export MICROBLOG_URL="https://micro.yourdomain.com"
    export MICROBLOG_TOKEN="<token from token.txt on the server>"

Direct usage:
    ./post.py "Just text, no image."
    ./post.py "Text with an image" --image ~/Pictures/photo.jpg
    ./post.py --show
    ./post.py --delete 13
    ./post.py --edit 24 "new text"

More convenient via the micropost wrapper:
    micropost hello world
    micropost show
    micropost del 13
    micropost edit 24 new text
"""
import argparse
import os
import sys

import requests


def main():
    parser = argparse.ArgumentParser(description="Post to / manage your microblog.")
    parser.add_argument("text", nargs="?", default="", help="Post text (default action: create a post)")
    parser.add_argument("--image", "-i", help="Path to an image")
    parser.add_argument("--url", default=os.environ.get("MICROBLOG_URL"), help="Server URL")
    parser.add_argument("--token", default=os.environ.get("MICROBLOG_TOKEN"), help="Auth token")
    parser.add_argument("--show", action="store_true", help="List the most recent posts")
    parser.add_argument("--limit", type=int, default=10, help="Number of posts for --show (default 10)")
    parser.add_argument("--delete", metavar="ID", help="Delete the post with this ID")
    parser.add_argument("--edit", metavar="ID", help="Edit the post with this ID (new text in the 'text' argument)")
    args = parser.parse_args()

    if not args.url or not args.token:
        sys.exit(
            "Error: MICROBLOG_URL and MICROBLOG_TOKEN must be set "
            "(as environment variables or via --url/--token)."
        )

    headers = {"Authorization": f"Bearer {args.token}"}
    base = args.url.rstrip("/")

    if args.show:
        resp = requests.get(f"{base}/api/posts", headers=headers, params={"limit": args.limit}, timeout=30)
        if resp.status_code != 200:
            sys.exit(f"Error ({resp.status_code}): {resp.text}")
        posts = resp.json()
        if not posts:
            print("(no posts)")
            return
        for p in posts:
            marker = f"  (edited {p['edited_at_display']})" if p.get("edited") else ""
            img = f"  [image: {p['image']}]" if p.get("image") else ""
            print(f"#{p['id']}  {p['created_at_display']}{marker}{img}")
            if p.get("text"):
                print(f"    {p['text']}")
        return

    if args.delete:
        post_id = args.delete.lstrip("#")
        resp = requests.post(f"{base}/api/delete/{post_id}", headers=headers, timeout=30)
        if resp.status_code == 200:
            print(f"#{post_id} deleted.")
        else:
            sys.exit(f"Error ({resp.status_code}): {resp.text}")
        return

    if args.edit:
        post_id = args.edit.lstrip("#")
        if not args.text:
            sys.exit("Error: please provide the new text (micropost edit N new text).")
        resp = requests.post(
            f"{base}/api/edit/{post_id}", headers=headers, data={"text": args.text}, timeout=30
        )
        if resp.status_code == 200:
            print(f"#{post_id} edited.")
        else:
            sys.exit(f"Error ({resp.status_code}): {resp.text}")
        return

    # Default action: create a new post
    text = args.text
    if not text and not args.image:
        sys.exit("Error: please provide text and/or --image.")

    data = {"text": text}
    files = None
    if args.image:
        if not os.path.isfile(args.image):
            sys.exit(f"Error: file not found: {args.image}")
        files = {"image": open(args.image, "rb")}

    try:
        resp = requests.post(f"{base}/api/post", headers=headers, data=data, files=files, timeout=30)
    finally:
        if files:
            files["image"].close()

    if resp.status_code == 201:
        info = resp.json()
        print(f"posted as #{info.get('id')} ({info.get('created_at_display', '')}).")
    else:
        sys.exit(f"Error ({resp.status_code}): {resp.text}")


if __name__ == "__main__":
    main()
