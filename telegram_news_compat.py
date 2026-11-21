"""Mirror the latest public Bee-Mine Telegram posts into a static JSON feed."""

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


CHANNEL = "BeeCraftWorld"
FEED_URL = f"https://t.me/s/{CHANNEL}"
POST_LIMIT = 3
VOID_ELEMENTS = set("area base br col embed hr img input link meta param source track wbr".split())


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect())


def fetch(url, limit, timeout=8):
    request = Request(url, headers={"User-Agent": "Bee-Mine news feed/1.0"})
    with OPENER.open(request, timeout=timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Telegram response exceeds size limit")
    return data


class Node:
    def __init__(self, tag, attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()

    def text(self):
        if self.tag == "br":
            return "\n"
        if self.tag == "img":
            return self.attrs.get("alt", "")
        if self.tag in {"script", "style"}:
            return ""
        content = "".join(child.text() if isinstance(child, Node) else child for child in self.children)
        return content + "\n" if self.tag in {"p", "blockquote"} else content


class TelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in VOID_ELEMENTS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def photo_details(nodes):
    for node in nodes:
        if not (node.has_class("tgme_widget_message_photo_wrap") or
                node.has_class("tgme_widget_message_video_thumb")):
            continue
        match = re.search(r"""url\(\s*['"]?(https://[^'"\s)]+)""", node.attrs.get("style", ""))
        if not match:
            continue
        url = match[1]
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if re.fullmatch(r"cdn\d+\.telesco\.pe", parts.netloc or ""):
            # Telegram includes a numeric media identifier among the classes.
            # Only reuse an old image when this identifier is still the same;
            # a removed or replaced attachment must not retain a stale image.
            media_id = next((part for part in node.attrs.get("class", "").split()
                             if re.fullmatch(r"\d{12,22}", part)), None)
            key = hashlib.sha256(media_id.encode()).hexdigest() if media_id else None
            return url, key
    return None, None


def photo_url(nodes):
    return photo_details(nodes)[0]


def parse_posts(html):
    parser = TelegramHTML()
    parser.feed(html)
    parser.close()
    posts = {}
    for node in parser.root.walk():
        match = re.fullmatch(CHANNEL + r"/([1-9]\d{0,11})", node.attrs.get("data-post", ""), re.I)
        if not match or not node.has_class("tgme_widget_message"):
            continue
        nodes = list(node.walk())
        date_link = next((n for n in nodes if n.has_class("tgme_widget_message_date")), None)
        stamp = next((n.attrs.get("datetime") for n in date_link.walk() if n.tag == "time"), None) if date_link else None
        try:
            date = datetime.strptime((stamp or "")[:-3] + (stamp or "")[-2:], "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            continue
        if date.tzinfo is None:
            continue
        copy = next((n for n in nodes if n.has_class("tgme_widget_message_text")), None)
        post_id = int(match[1])
        source_photo, photo_key = photo_details(nodes)
        posts[post_id] = {
            "id": post_id,
            "published": date.isoformat(),
            "text": copy.text().strip()[:16000] if copy else "",
            "source_photo": source_photo,
            "source_photo_key": photo_key,
        }
    return sorted(posts.values(), key=lambda post: post["id"], reverse=True)[:POST_LIMIT]


def publish(path, data):
    if path.exists() and path.read_bytes() == data:
        return
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
            staged = Path(temp.name)
            temp.write(data)
        staged.chmod(0o644)
        os.replace(staged, path)
    finally:
        if staged is not None:
            staged.unlink() if staged.exists() else None


def compressed_photo(directory, url, original_name):
    """Create a small, immutable WebP preview; retain the original on failure."""
    digest = hashlib.sha256(("webp640q55:v1\0" + url).encode()).hexdigest()
    name = "photo-" + digest + ".webp"
    target = directory / name
    if target.is_file() and target.stat().st_size:
        return name
    try:
        original = directory / original_name
        if original.stat().st_size > 1_500_000:
            raise ValueError("Image exceeds size limit")
        data = original.read_bytes()
        # Do not let ImageMagick auto-detect other formats/delegates from the input.
        if original.suffix.lower() == ".jpg" and data.startswith(b"\xff\xd8\xff"):
            input_type = "jpeg:-"
        elif original.suffix.lower() == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
            input_type = "png:-"
        elif (original.suffix.lower() == ".webp" and data.startswith(b"RIFF")
              and data[8:12] == b"WEBP"):
            input_type = "webp:-"
        else:
            raise ValueError("Unsupported image format")
        result = subprocess.run(
            ["/usr/bin/convert", "-limit", "memory", "64MiB", "-limit", "map", "128MiB",
             "-limit", "disk", "0", "-limit", "area", "16000000", "-limit", "thread", "1",
             input_type, "-auto-orient", "-resize", "640x640>", "-strip", "-quality", "55", "webp:-"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=True)
        data = result.stdout
        if len(data) < 12 or not data.startswith(b"RIFF") or data[8:12] != b"WEBP":
            raise ValueError("Invalid WebP output")
        # Keep the original when it is already smaller than our preview.
        if len(data) >= original.stat().st_size:
            return original_name
        publish(target, data)
        return name
    except Exception as exc:
        # Covers missing ImageMagick/WebP support and malformed/unsupported images.
        # Do not print image URLs, as they can contain changing tokens.
        logging.warning("Telegram image compression failed: %s", type(exc).__name__)
        return original_name


def mirror_photo(directory, url):
    stem = "photo-" + hashlib.sha256(url.encode()).hexdigest()
    preview = directory / ("photo-" + hashlib.sha256(("webp640q55:v1\0" + url).encode()).hexdigest() + ".webp")
    if preview.is_file() and preview.stat().st_size:
        return preview.name
    existing = next((directory / (stem + suffix) for suffix in (".jpg", ".png", ".webp")
                     if (directory / (stem + suffix)).is_file()), None)
    if existing:
        return compressed_photo(directory, url, existing.name)
    for attempt in range(2):
        try:
            data = fetch(url, 1_500_000, timeout=5)
            break
        except (OSError, ValueError) as exc:
            # URLs can contain changing tokens; do not put them in logs.
            logging.warning("Telegram image fetch attempt %d failed: %s", attempt + 1, type(exc).__name__)
            if attempt == 0:
                time.sleep(0.5)
    else:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        logging.warning("Telegram image has an unsupported format")
        return None
    name = stem + suffix
    publish(directory / name, data)
    return compressed_photo(directory, url, name)


def sync(directory):
    posts = parse_posts(fetch(FEED_URL, 2_000_000).decode("utf-8"))
    if not posts:
        raise ValueError("No public Bee-Mine posts found; keeping the last feed")
    try:
        previous = json.loads((directory / "latest.json").read_text(encoding="utf-8"))
        previous_posts = {p.get("id"): p for p in previous.get("posts", []) if isinstance(p, dict)}
    except (OSError, ValueError, AttributeError, TypeError):
        previous_posts = {}
    images = set()
    for post in posts:
        source = post.pop("source_photo")
        key = post.pop("source_photo_key")
        name = mirror_photo(directory, source) if source else None
        old = previous_posts.get(post["id"], {})
        old_name = old.get("photo", "")
        # A safe, existing previous file may be reused only for the same media.
        # On the first run after upgrading, the old filename identifies the URL.
        same_url = (isinstance(old_name, str) and source and old_name.startswith(
            "photo-" + hashlib.sha256(source.encode()).hexdigest() + "."))
        same_media = key and key == old.get("photo_key")
        # Telegram may temporarily return a post without media markup. Keep a
        # known image briefly, then drop it if the omission persists.
        missing_until = old.get("photo_missing_until", 0)
        if not name and old_name and old.get("text") == post["text"]:
            if not isinstance(missing_until, (int, float)) or not missing_until:
                missing_until = int(time.time()) + 3600
            if time.time() < missing_until:
                post["photo_missing_until"] = missing_until
                same_media = True
        if (not name and (same_media or same_url)
                and isinstance(old_name, str)
                and re.fullmatch(r"photo-[0-9a-f]{64}\.(?:jpg|png|webp)", old_name)
                and (directory / old_name).is_file()):
            name = old_name
            logging.warning("Keeping previous image for post %s after fetch failure", post["id"])
        if name:
            post["photo"] = name
            if key:
                post["photo_key"] = key
            images.add(name)
    payload = json.dumps({"posts": posts}, ensure_ascii=False, separators=(",", ":"))
    publish(directory / "latest.json", payload.encode("utf-8"))
    cutoff = time.time() - 86400
    for image in directory.glob("photo-*"):
        if image.name not in images and image.stat().st_mtime < cutoff:
            image.unlink()


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("directory", type=Path)
    sync(cli.parse_args().directory)

