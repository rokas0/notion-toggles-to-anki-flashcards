# Notion Toggles to Flashcards

**An Anki add-on that turns the toggle blocks in your Notion pages into
flashcards and keeps them in sync.** The toggle's title is the front of the
card and its content is the back. Keep writing in Notion, and your cards
stay up to date.

## Features

- Toggle blocks → Question/Answer notes. If a toggle has no content and its
  title contains `{{c1::…}}`, it becomes a Cloze note.
- Tags: a paragraph in the toggle made only of tags (e.g. `#aws #iam`) becomes the note's tags.
- Inline and block LaTeX, images, tables and code.
- Toggles whose title starts with ❕ are ignored.
- Subdecks via `::` (e.g. `Languages::English`); several pages can share a deck.
- Optionally, each subpage of a page becomes its own subdeck (`AWS::IAM::Policies`).
- Nested toggles stay collapsible on the card and also become cards of their own.
- Background sync on an interval. You can optionally skip pages that haven't changed.
- Manual sync. Optionally it also handles cards whose toggle was deleted in Notion: it lists them first, and by default suspends and tags them rather than deleting them.
- All pages are exported in one batch with backoff, so you stay within Notion's rate limits.

## Installation

1. Download or clone this repository into your Anki add-ons folder as
   `notion_toggles_to_flashcards`. To find the add-ons folder, open Anki and go to
   **Tools > Add-ons > View Files**.
2. Restart Anki.
3. Go to **Tools > Add-ons > Notion Toggles to Flashcards > Config** and fill in your tokens and pages (see below).

## Configuration

```json
{
  "sync_every_minutes": 30,
  "notion_token": "<token_v2 cookie>",
  "file_token": "<file_token cookie>",
  "notion_namespace": "<your Notion user name>",
  "notion_pages": [
    { "page_id": "<32 char page id>", "recursive": true, "target_deck": "Notion" }
  ]
}
```

All options, and how to get the tokens and page ids, are documented in
[`config.md`](config.md). Anki also shows that file next to the config editor.

> ⚠️ `token_v2` and `file_token` give full access to your Notion account.
> Never commit them or share a debug log that contains them.

## Usage

- **Tools > Notion Toggles to Flashcards > Sync from Notion**: manual sync.
- **Tools > Notion Toggles to Flashcards > Sync from Notion and remove deleted notes…**: sync,
  then list the cards whose toggle no longer exists in Notion and offer to suspend them (tagged
  `notion-deleted`) or, with `"remove_mode": "delete"`, delete them. Decks with a failed or
  skipped page are never touched.
- A background sync runs at startup and then every `sync_every_minutes`.
- The top of the menu shows when the last sync ran and what it changed.

Cards are matched to Notion toggles by the toggle's ID, so you can edit the
question *or* the answer in Notion, or move the toggle to another page, and the
existing card is updated and keeps its review history. Each card's Source link
jumps straight to its toggle in Notion.

When Notion rejects your login cookies (they expire every few months), the
add-on tells you which one and how to renew it.

## How it works

This add-on uses Notion's **private** web API (the same one the Notion web app
uses to "Export → HTML") with your browser cookies. It does not use the
official public API. That API can change without notice; if syncing suddenly
breaks, please open an issue.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The tests cover the HTML parser, note matching, the config schema and the sync
loop. They don't need Anki to be installed.

To build the `.ankiaddon` package for AnkiWeb:

```sh
python scripts/build_ankiaddon.py   # -> dist/notion_toggles_to_flashcards.ankiaddon
```

CI runs the tests on Python 3.9 and 3.13 for every push and pull request.
Pushing a tag like `v1.0.0` also attaches the built package to a GitHub release.

## Credits and licence

This add-on started as a fork of
[Notion2Anki](https://github.com/BaiRuic/Notion2Anki), which is based on
[notion-anki-sync](https://github.com/9dogs/notion-anki-sync) by 9dogs.
Cards created by those add-ons are picked up as they are, because it
keeps their note types (`notion-anki-sync`, `notion-anki-sync-cloze`). Like the original, it is licensed under the
**GNU General Public License v3.0** (see [`LICENSE`](LICENSE)).
