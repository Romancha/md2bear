# md2bear

Convert markdown notes to [TextBundle](http://textbundle.org/) format for [Bear](https://bear.app/) import.

Works with exports from **Notesnook**, **Obsidian**, **Joplin**, and other markdown-based note apps.

## Features

- Converts `.md` files to `.textbundle` (Bear's native import format)
- Fixes image/attachment paths and copies files into bundles
- Converts folder structure to Bear hashtags (e.g. `Dev/Docker/` → `#Dev #Docker`)
- Preserves existing frontmatter tags as Bear hashtags
- Strips YAML frontmatter from output (Bear doesn't render it)
- Preserves original creation/modification dates
- Handles wiki-links (`![[file]]`) and standard markdown links
- Converts `.md` links to wiki-links (`[[title]]`)
- Handles duplicate note names across different folders
- Replaces spaces in tags with underscores for Bear compatibility

### HTML cleanup (Notesnook-specific)

- Converts HTML `<a href>` links to markdown links
- Strips `<div>` wrappers (with `data-block-id`, `style`, etc.)
- Replaces `&nbsp;` / `&#160;` with regular spaces
- Removes angle brackets from URLs: `](<https://...>)` → `](https://...)`
- Neutralizes `#`-anchor links that Bear would misinterpret as tags

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

```bash
uv sync
```

## Usage

```bash
uv run python md2bear.py /path/to/notes
```

Output will be created at `/path/to/notes-textbundle/`.

### Options

```
uv run python md2bear.py /path/to/notes                                   # basic conversion
uv run python md2bear.py /path/to/notes -o /path/to/out                   # custom output directory
uv run python md2bear.py /path/to/notes --no-tags                         # skip tag generation
uv run python md2bear.py /path/to/notes --flat-tags                       # flat tags (#Dev #Docker) instead of nested (#Dev/Docker)
uv run python md2bear.py /path/to/notes --skip-folders "Inbox" "Archive"  # custom folders to skip
```

### Import into Bear

1. Run the script
2. Import using one of two methods:
   - **File → Import → Import From → Markdown Folder** — select the output folder. Bear will import all notes but auto-create a `#folder` tag (right-click it in sidebar → Delete Tag after import)
   - **File → Import Notes** — select all `.textbundle` files inside the output folder

## Expected input structure

```
notes/
├── attachments/          # images and files
│   ├── abc123-photo.jpg
│   └── def456-image.png
├── Dev/
│   ├── Docker/
│   │   └── Docker-notes.md
│   └── Python/
│       └── Tips.md
├── Personal/
│   └── Recipe.md
└── My-note.md
```

## Output structure

```
notes-textbundle/
├── Docker-notes.textbundle/
│   ├── text.md        # cleaned markdown with #Dev #Docker tags
│   ├── info.json
│   └── assets/
│       └── image.png
├── Tips.textbundle/
│   ├── text.md        # with #Dev #Python tags
│   ├── info.json
│   └── assets/
└── ...
```

## Tested with

- [Notesnook](https://notesnook.com/) markdown export (primary target)
- Generic markdown notes with YAML frontmatter

## License

MIT
