#!/usr/bin/env python3
"""Convert markdown notes to TextBundle format for Bear import.

Handles:
- Image/attachment path fixing → assets/
- Wiki-links (![[file]]) and standard markdown links
- Folder structure → Bear hashtags (flat tags)
- Existing frontmatter tags → Bear hashtags
- Spaces in tags replaced with underscores
- Original created/updated dates preserved via file timestamps
- YAML frontmatter stripped from output
- Anchor links neutralized to prevent false Bear tags
- Markdown file links converted to wiki-links ([[title]])
- Duplicate note name handling
"""

import argparse
import json
import os
import re
import shutil
import urllib.parse
from datetime import date, datetime
from pathlib import Path

import yaml

ASSET_DIRS = {"attachments", "_resources", "resources", "assets", "media", "images", "files"}

# Magic bytes → extension mapping for files without extensions
MAGIC_BYTES = [
    (b'\x89PNG\r\n\x1a\n', '.png'),
    (b'\xff\xd8\xff', '.jpg'),
    (b'GIF87a', '.gif'),
    (b'GIF89a', '.gif'),
    (b'RIFF', '.webp'),  # RIFF....WEBP (check further below)
    (b'BM', '.bmp'),
    (b'\x00\x00\x01\x00', '.ico'),
    (b'%PDF', '.pdf'),
]


def detect_extension(file_path: Path) -> str | None:
    """Detect file type by magic bytes and return appropriate extension."""
    try:
        with open(file_path, 'rb') as f:
            header = f.read(12)
    except OSError:
        return None
    for magic, ext in MAGIC_BYTES:
        if header.startswith(magic):
            # RIFF can be WAV or WEBP — check bytes 8-12
            if magic == b'RIFF':
                if header[8:12] == b'WEBP':
                    return '.webp'
                continue
            return ext
    return None

LINKS_PATTERN = re.compile(r'(\[.*?\]\(((?:[^()]|\((?:[^()]*\)))+)\))')
WIKI_LINKS_PATTERN = re.compile(r'(\!\[\[(.*?)\]\])')
WIKI_NOTE_LINKS_PATTERN = re.compile(r'(?<!\!)\[\[(.*?)\]\]')


def parse_frontmatter(content: str) -> tuple[dict | None, int]:
    """Parse YAML frontmatter, return (data, end_index)."""
    if not content.startswith('---'):
        return None, 0
    end = content.find('---', 3)
    if end < 0:
        return None, 0
    try:
        data = yaml.safe_load(content[3:end])
    except yaml.YAMLError:
        return None, 0
    return (data if isinstance(data, dict) else None), end + 3


def parse_date(date_str: str) -> float | None:
    """Parse date string in various formats. Supports ISO 8601 with timezone/fractional seconds."""
    if not date_str or not isinstance(date_str, str):
        return None
    s = date_str.strip()

    # Try Python's fromisoformat first (handles ISO 8601 with tz, fractional seconds)
    # Normalize 'Z' suffix to '+00:00' for compatibility with Python < 3.11
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except ValueError:
        pass

    # Additional non-ISO formats
    formats = [
        "%d-%m-%Y %I:%M %p",  # Notesnook: DD-MM-YYYY HH:MM AM/PM
        "%d-%m-%Y %H:%M",     # DD-MM-YYYY HH:MM (24h)
        "%Y-%m-%d",           # Bare date
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def get_dates(front_matter: dict | None, md_path: Path) -> tuple[float, float]:
    """Extract timestamps from frontmatter or fall back to file times."""
    created = None
    updated = None
    if front_matter:
        for key in ('created_at', 'created', 'date'):
            val = front_matter.get(key)
            if val:
                if isinstance(val, datetime):
                    created = val.timestamp()
                elif isinstance(val, date):
                    created = datetime(val.year, val.month, val.day).timestamp()
                else:
                    created = parse_date(str(val))
                if created:
                    break
        for key in ('updated_at', 'updated', 'modified'):
            val = front_matter.get(key)
            if val:
                if isinstance(val, datetime):
                    updated = val.timestamp()
                elif isinstance(val, date):
                    updated = datetime(val.year, val.month, val.day).timestamp()
                else:
                    updated = parse_date(str(val))
                if updated:
                    break
    if not created:
        created = os.path.getmtime(md_path)
    if not updated:
        updated = created
    return created, updated


def get_folder_tags(md_path: Path, notes_dir: Path, skip_folders: set[str]) -> list[str]:
    """Extract tags from folder hierarchy, skipping service folders."""
    rel = md_path.relative_to(notes_dir)
    parts = list(rel.parent.parts)
    return [p for p in parts if p not in skip_folders]


def get_frontmatter_tags(front_matter: dict | None) -> list[str]:
    """Extract tags from frontmatter."""
    if not front_matter:
        return []
    tags = front_matter.get('tags', [])
    if isinstance(tags, str):
        if not tags.strip():
            return []
        return [t.strip() for t in tags.split(",") if t.strip()]
    if isinstance(tags, list):
        return [str(t).strip() for t in tags if str(t).strip()]
    return []


def build_file_map(notes_dir: Path) -> dict[str, Path]:
    """Build a global filename → path map for attachment lookup."""
    file_map = {}
    for f in notes_dir.rglob("*"):
        if f.is_file():
            file_map[f.name] = f
    return file_map


def find_file(original_path: Path, file_map: dict[str, Path]) -> Path | None:
    """Find a file by exact path or by name in global map."""
    if original_path.exists():
        return original_path
    name = original_path.name
    return file_map.get(name)


def convert_note(
    md_path: Path,
    notes_dir: Path,
    output_dir: Path,
    file_map: dict[str, Path],
    used_names: set[str],
    add_tags: bool,
    skip_folders: set[str],
    nested_tags: bool = False,
):
    bundle_name = md_path.stem
    if bundle_name in used_names:
        rel_parent = md_path.relative_to(notes_dir).parent
        suffix = rel_parent.as_posix().replace("/", "_").replace(" ", "_")
        bundle_name = f"{bundle_name}_{suffix}"
    used_names.add(bundle_name)

    bundle_path = output_dir / (bundle_name + ".textbundle")
    assets_path = bundle_path / "assets"
    os.makedirs(assets_path, exist_ok=True)

    content = md_path.read_text(encoding="utf-8")

    # Parse frontmatter
    front_matter, fm_end = parse_frontmatter(content)
    created_ts, updated_ts = get_dates(front_matter, md_path)

    # Collect tags
    all_tags = []
    if add_tags:
        folder_tags = get_folder_tags(md_path, notes_dir, skip_folders)
        frontmatter_tags = get_frontmatter_tags(front_matter)

        if nested_tags and folder_tags:
            # Build nested tag: #Dev/Docker/Compose
            nested = "/".join(t.strip().replace(" ", "_") for t in folder_tags)
            all_tags.append(nested)
            seen = {nested.lower()}
        else:
            seen = set()
            for tag in folder_tags:
                tag = tag.strip().replace(" ", "_")
                if not tag:
                    continue
                tag_lower = tag.lower()
                if tag_lower not in seen:
                    seen.add(tag_lower)
                    all_tags.append(tag)

        # Frontmatter tags always added as flat
        for tag in frontmatter_tags:
            tag = tag.strip().replace(" ", "_")
            if not tag:
                continue
            tag_lower = tag.lower()
            if tag_lower not in seen:
                seen.add(tag_lower)
                all_tags.append(tag)

    # Strip frontmatter
    if fm_end > 0:
        content = content[fm_end:].lstrip("\n")

    # Ensure title exists (Bear uses first line as title)
    title = front_matter.get("title", md_path.stem) if front_matter else md_path.stem
    lines = content.splitlines()
    first_content_line = ""
    for line in lines:
        if line.strip():
            first_content_line = re.sub(r'^#+\s*', '', line.strip())
            break
    if first_content_line != title and title:
        content = f"# {title}\n\n{content}"

    new_content = content
    copied_assets = set()

    def copy_to_assets(found_path: Path) -> str:
        """Copy file to assets/ and return the quoted filename.
        If the file has no extension, detect type by magic bytes and add one."""
        name = found_path.name
        if not found_path.suffix:
            ext = detect_extension(found_path)
            if ext:
                name = name + ext
        dest = assets_path / name
        if name not in copied_assets:
            shutil.copy2(found_path, dest)
            copied_assets.add(name)
        return urllib.parse.quote(name)

    # Process standard markdown links — copy local files to assets
    def process_link(match):
        nonlocal new_content
        full = match.group(1)
        link = match.group(2)
        if not link:
            return
        # Strip angle brackets: <path> → path (common in Notesnook exports)
        clean_link = link.strip('<>')
        if clean_link.startswith(('#', 'http://', 'https://')):
            return
        if re.match(r'^[a-zA-Z\-0-9.]+:', clean_link):
            return
        unquoted = urllib.parse.unquote(clean_link)
        basename = os.path.basename(unquoted)
        ext = os.path.splitext(basename)[1].lower()

        # Convert .md links to wiki-links
        if ext == '.md':
            title = os.path.splitext(basename)[0]
            escaped = re.escape(link)
            new_content = re.sub(rf'!\[.*?\]\({escaped}\)', f'[[{title}]]', new_content)
            new_content = re.sub(rf'\[.*?\]\({escaped}\)', f'[[{title}]]', new_content)
            return

        # Copy file to assets
        file_path = md_path.parent / unquoted
        found = find_file(file_path, file_map)
        if found:
            quoted_name = copy_to_assets(found)
            new_content = new_content.replace(full, full.replace(link, f"assets/{quoted_name}"))

    for m in LINKS_PATTERN.finditer(new_content):
        process_link(m)

    # Process wiki-links for embeds (![[file]] and ![[file|size]])
    for m in WIKI_LINKS_PATTERN.finditer(new_content):
        full = m.group(1)
        raw_link = m.group(2)

        # Strip Obsidian size hint or alias: ![[image.png|300]] → image.png
        link = raw_link.split('|')[0].strip()
        unquoted = urllib.parse.unquote(link)
        file_path = md_path.parent / unquoted
        found = find_file(file_path, file_map)

        # Check if it's a link to another .md note
        if not found and find_file(Path(str(file_path) + ".md"), file_map):
            new_content = new_content.replace(full, f"[[{unquoted}]]")
            continue

        if found:
            quoted_name = copy_to_assets(found)
            new_content = new_content.replace(full, f"![](assets/{quoted_name})")

    # Process wiki-links for note references ([[Note]] and [[Note|Alias]])
    for m in WIKI_NOTE_LINKS_PATTERN.finditer(new_content):
        full = m.group(0)
        raw_link = m.group(1)
        # Strip alias: [[Note Title|Display Text]] → Note Title
        link = raw_link.split('|')[0].strip()
        if link != raw_link:
            new_content = new_content.replace(full, f"[[{link}]]")

    # Convert HTML <a> links to markdown: <a href="url" ...>text</a> → [text](url)
    new_content = re.sub(r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>', r'[\2](\1)', new_content)

    # Strip Notesnook HTML div wrappers (data-block-id, style, etc.)
    # Repeat to handle nested divs (inner first, then outer)
    for _ in range(3):
        new_content = re.sub(r'<div[^>]*><br></div>', '', new_content)
        new_content = re.sub(r'<div[^>]*>(.*?)</div>', r'\1', new_content)

    # Replace HTML non-breaking spaces with regular spaces
    new_content = new_content.replace('&nbsp;', ' ').replace('&#160;', ' ')

    # Fix angle-bracket URLs: ](<https://...>) → ](https://...)
    # Bear doesn't support angle brackets around URLs in links
    new_content = re.sub(r'\]\(<(https?://[^>]+)>\)', r'](\1)', new_content)

    # Neutralize anchor links with # that Bear interprets as tags
    new_content = re.sub(r'\(<#([^)]+)\)', r'(\1)', new_content)
    new_content = re.sub(r'<#(\w+)', '<\uff03\\1', new_content)

    # Neutralize stray # outside code blocks that Bear would interpret as tags
    # Replace # with fullwidth ＃ (U+FF03) — visually similar but Bear ignores it
    # Process line by line, skip code blocks and markdown headings
    lines = new_content.split('\n')
    result_lines = []
    in_code_block = False
    for line in lines:
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
        if not in_code_block:
            # Skip markdown headings (# Title)
            stripped = line.lstrip()
            is_heading = stripped.startswith('#') and (len(stripped) == 1 or stripped[1] == ' ' or
                          (stripped.startswith('##') and (len(stripped) == 2 or stripped[2] in (' ', '#'))))
            if not is_heading:
                # Replace any # followed by non-space with fullwidth ＃
                line = re.sub(r'#(?=[^\s#])', '\uff03', line)
        result_lines.append(line)
    new_content = '\n'.join(result_lines)

    # Append Bear tags
    if all_tags:
        tag_line = " ".join(f"#{tag}" for tag in all_tags)
        new_content = new_content.rstrip("\n") + "\n\n" + tag_line + "\n"

    # Write text.md
    text_md_path = bundle_path / "text.md"
    text_md_path.write_text(new_content, encoding="utf-8")

    # Write info.json
    info = {"version": 2, "type": "net.daringfireball.markdown", "transient": False}
    (bundle_path / "info.json").write_text(json.dumps(info), encoding="utf-8")

    # Preserve timestamps
    os.utime(text_md_path, (created_ts, updated_ts))
    os.utime(bundle_path, (created_ts, updated_ts))

    return len(copied_assets), all_tags


def main():
    parser = argparse.ArgumentParser(
        description="Convert markdown notes to TextBundle format for Bear import."
    )
    parser.add_argument("input_dir", help="Path to markdown notes directory")
    parser.add_argument(
        "-o", "--output",
        help="Output directory (default: <input_dir>-textbundle)",
    )
    parser.add_argument(
        "--tags", action="store_true", default=True,
        help="Convert folder structure and frontmatter tags to Bear hashtags (default: on)",
    )
    parser.add_argument(
        "--no-tags", action="store_false", dest="tags",
        help="Do not add any tags",
    )
    parser.add_argument(
        "--skip-folders",
        nargs="*",
        default=[],
        help="Folder names to skip when generating tags (e.g. --skip-folders 'All notes' 'General')",
    )
    parser.add_argument(
        "--nested-tags", action="store_true", default=True,
        help="Use nested tags from folder path (e.g. #Dev/Docker) (default: on)",
    )
    parser.add_argument(
        "--flat-tags", action="store_false", dest="nested_tags",
        help="Use flat tags instead of nested (e.g. #Dev #Docker)",
    )
    args = parser.parse_args()

    notes_dir = Path(args.input_dir).resolve()
    if not notes_dir.is_dir():
        print(f"Error: {notes_dir} is not a directory")
        return

    output_dir = Path(args.output).resolve() if args.output else notes_dir.parent / (notes_dir.name + "-textbundle")

    skip_folders = set(args.skip_folders) if args.skip_folders else set()

    if output_dir.exists():
        answer = input(f"Output directory already exists: {output_dir}\nRemove and recreate? [y/N] ")
        if answer.lower() != 'y':
            print("Aborted.")
            return
        shutil.rmtree(output_dir)
    os.makedirs(output_dir)

    print(f"Input:  {notes_dir}")
    print(f"Output: {output_dir}")
    print(f"Tags:   {'nested' if args.nested_tags else 'flat' if args.tags else 'off'}")
    if skip_folders:
        print(f"Skip:   {', '.join(sorted(skip_folders))}")
    print()

    # Build global file map for attachment lookup
    file_map = build_file_map(notes_dir)

    md_files = [
        f for f in notes_dir.rglob("*.md")
        if not (ASSET_DIRS & set(f.relative_to(notes_dir).parts))
    ]

    print(f"Found {len(md_files)} markdown files")

    used_names: set[str] = set()
    notes_with_images = 0
    notes_with_tags = 0

    for md in sorted(md_files):
        attachments_count, tags = convert_note(
            md, notes_dir, output_dir, file_map, used_names, args.tags, skip_folders, args.nested_tags
        )
        if attachments_count > 0:
            notes_with_images += 1
        if tags:
            notes_with_tags += 1

    print(f"Converted {len(md_files)} notes to TextBundle")
    print(f"Notes with images: {notes_with_images}")
    print(f"Notes with tags: {notes_with_tags}")
    print(f"\nDone! Import .textbundle files from: {output_dir}")


if __name__ == "__main__":
    main()
