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
from datetime import datetime
from pathlib import Path

import yaml

DEFAULT_SKIP_FOLDERS = {"All notes", "General"}

IMAGE_PATTERN = re.compile(
    r'(!\[[^\]]*\]\()(<?)(\./(?:\.\./)*attachments/([^>)\s]+))(>?\))'
)
LINKS_PATTERN = re.compile(r'(\[.*?\]\(((?:[^()]|\((?:[^()]*\)))+)\))')
WIKI_LINKS_PATTERN = re.compile(r'(\!\[\[(.*?)\]\])')


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
    """Try multiple date formats."""
    if not date_str or not isinstance(date_str, str):
        return None
    formats = [
        "%d-%m-%Y %I:%M %p",  # Notesnook: DD-MM-YYYY HH:MM AM/PM
        "%Y-%m-%d %H:%M:%S",  # ISO-like
        "%Y-%m-%dT%H:%M:%S",  # ISO
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt).timestamp()
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
                else:
                    created = parse_date(str(val))
                if created:
                    break
        for key in ('updated_at', 'updated', 'modified'):
            val = front_matter.get(key)
            if val:
                if isinstance(val, datetime):
                    updated = val.timestamp()
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

    # Fix Notesnook-style attachment paths
    used_attachments = []

    def replace_notesnook_path(match):
        prefix = match.group(1)
        filename = match.group(4)
        used_attachments.append(filename)
        return f"{prefix}assets/{filename})"

    new_content = IMAGE_PATTERN.sub(replace_notesnook_path, content)

    # Process standard markdown links — copy local files to assets
    def process_link(match):
        nonlocal new_content
        full = match.group(1)
        link = match.group(2)
        if not link or link.startswith(('#', 'http://', 'https://')):
            return
        if re.match(r'^[a-zA-Z\-0-9.]+:', link):
            return

        unquoted = urllib.parse.unquote(link)
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
            quoted_name = urllib.parse.quote(found.name)
            shutil.copy2(found, assets_path / found.name)
            new_content = new_content.replace(full, full.replace(link, f"assets/{quoted_name}"))

    for m in LINKS_PATTERN.finditer(new_content):
        process_link(m)

    # Process wiki-links (![[file]])
    for m in WIKI_LINKS_PATTERN.finditer(new_content):
        full = m.group(1)
        link = m.group(2)
        unquoted = urllib.parse.unquote(link)
        file_path = md_path.parent / unquoted
        found = find_file(file_path, file_map)

        if not found and find_file(Path(str(file_path) + ".md"), file_map):
            new_content = new_content.replace(full, f"[[{unquoted}]]")
            continue

        if found:
            quoted_name = urllib.parse.quote(found.name)
            shutil.copy2(found, assets_path / found.name)
            new_content = new_content.replace(full, f"![](assets/{quoted_name})")

    # Copy Notesnook attachments
    attachments_dir = notes_dir / "attachments"
    for filename in used_attachments:
        src = attachments_dir / filename
        if src.exists():
            shutil.copy2(src, assets_path / filename)

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

    return len(used_attachments), all_tags


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
        default=["All notes", "General"],
        help="Folder names to skip when generating tags (default: 'All notes' 'General')",
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
        if "attachments" not in f.parts and f.name != "md2bear.py"
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
