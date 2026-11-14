# Configuration

- `debug`: `bool [default: false]`: write a debug log to `log.txt` in the add-on folder. Tokens are masked, but the log contains page titles and note text.
- `sync_every_minutes`: `int [default: 30]`: auto-sync interval in minutes. Set to `0` to disable the timer (a sync still runs once at startup).
- `remove_mode`: `"suspend" | "delete" [default: "suspend"]`: what **Sync from Notion and remove deleted notes…** does with cards whose toggle is gone. `suspend` suspends their cards and tags them `notion-deleted`, so nothing is lost; if the toggle comes back, the card is unsuspended. `delete` deletes the notes and their review history. Either way you see the list and confirm first.
- `skip_unchanged_pages`: `bool [default: false]`: background syncs skip pages whose *last edited* time hasn't changed since the last successful sync. Recursive pages are never skipped, because editing a subpage doesn't change its parent's time. Manual syncs always export everything, and decks with skipped pages are never pruned.
- `timezone`: `str [default: ""]`: IANA time zone Notion uses for dates in exports, e.g. `Europe/London`. Empty means your computer's time zone.
- `sequential_sync`: `bool [default: false]`: export one page at a time. Only turn this on if you still hit rate limits.
- `worker_stagger_delay`: `int [default: 1]`: seconds between starting page exports.
- `notion_token`: `str`: your Notion `token_v2` cookie (see below).
- `file_token`: `str`: your `file_token` cookie from `file.notion.com`, needed to download exports and images.
- `notion_namespace`: `str`: your Notion workspace or user name, used to build the "Source" links.
- `notion_pages`: `array [default: []]`: pages to load notes from.
  - `page_id`: the 32-character Notion page id.
  - `recursive`: if `true`, subpages are included.
  - `subdecks`: if `true` (with `recursive`), each subpage's notes go into a subdeck named after it, e.g. `AWS::IAM::Policies`. Existing cards are moved there and keep their history. If you turn it off again, cards already in subdecks stay where they are and keep syncing.
  - `target_deck`: deck to put the notes in. Use `::` for subdecks (e.g. `"Languages::English"`). Several pages may share a deck.

## Notion token

Log in to Notion in a browser and open Developer Tools (`Cmd+Opt+I` / `Ctrl+Shift+I`). Go to **Application > Cookies**:

- `https://www.notion.so` → copy `token_v2` into `notion_token`
- `https://file.notion.com` → copy `file_token` into `file_token`

Treat both like passwords: anyone holding them can read your whole Notion workspace.

When a token expires, the add-on stops the sync and shows these steps for
the token Notion rejected. Background syncs show the message once per Anki
session; the menu shows it too.

## Notion pages

The page id is the 32 characters at the end of the page URL:
`https://www.notion.so/notion_user/My-Learning-Book-8a775ee482ab43732abc9319add819c5`
➡ `8a775ee482ab43732abc9319add819c5`

## Rate limiting

All pages are exported in one batch: the add-on starts every export, then checks on all of them with a single request. The interval grows from 2 s to 10 s, and the add-on backs off when Notion asks it to (HTTP 429 / `Retry-After`). If you still see rate-limit errors in the log:

1. enable `skip_unchanged_pages`,
2. increase `worker_stagger_delay`,
3. as a last resort, enable `sequential_sync`.
