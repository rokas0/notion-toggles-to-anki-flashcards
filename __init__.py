"""Notion Toggles to Flashcards.

Syncs toggle blocks from Notion pages into Anki notes.
"""

import json
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, cast

from aqt import mw
from aqt.gui_hooks import main_window_did_init, profile_will_close
from aqt.utils import showCritical, showInfo
from jsonschema import ValidationError, validate
from PyQt6.QtCore import QObject, QThreadPool, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMenu, QMessageBox

from .helpers import (
    BASE_DIR,
    USER_FILES_DIR,
    enable_logging_to_file,
    get_logger,
    local_timezone,
    normalize_block_id,
)
from .notes_manager import REMOVED_TAG, NotesManager
from .parser import AnkiNote
from .sync_worker import PageJob, SyncWorker

#: Where `last_edited_time` of every successfully synced page is kept
STATE_FILE = USER_FILES_DIR / "sync_state.json"
#: Config keys that must never be written to the log
SECRET_KEYS = ("notion_token", "file_token")
MENU_TITLE = "📇 Notion Toggles to Flashcards"
ADDON_NAME = "Notion Toggles to Flashcards"
#: Most questions listed in the "remove deleted notes" confirmation
MAX_LISTED_REMOVALS = 200

#: How to renew each cookie, shown when Notion rejects it
TOKEN_HELP = {
    "notion_token": (
        "Notion rejected your login token (notion_token). It has probably "
        "expired; Notion logins last a few months.\n\n"
        "To renew it:\n"
        "1. Log in to notion.so in your browser.\n"
        "2. Open Developer Tools (Cmd+Opt+I / Ctrl+Shift+I) > Application "
        "> Cookies > https://www.notion.so.\n"
        "3. Copy the value of token_v2.\n"
        "4. In Anki: Tools > Add-ons > " + ADDON_NAME + " > Config, paste "
        "it as notion_token, then sync again."
    ),
    "file_token": (
        "Notion rejected your download token (file_token), so the export "
        "could not be downloaded. It has probably expired.\n\n"
        "To renew it:\n"
        "1. Log in to notion.so in your browser and open any image or file "
        "so the cookie is set.\n"
        "2. Open Developer Tools > Application > Cookies > "
        "https://file.notion.com.\n"
        "3. Copy the value of file_token.\n"
        "4. In Anki: Tools > Add-ons > " + ADDON_NAME + " > Config, paste "
        "it as file_token, then sync again."
    ),
}


class NotionSyncPlugin(QObject):
    """Reads config, owns the menu and timer and applies synced notes."""

    #: Default sync interval, min
    DEFAULT_SYNC_INTERVAL: int = 30

    def __init__(self) -> None:
        super().__init__()
        # While testing `mw` is None
        if not mw:
            return
        mw.addonManager.setConfigUpdatedAction(__name__, self.reload_config)
        self.config = self.get_valid_config(mw.addonManager.getConfig(__name__))
        self.debug = bool(self.config.get("debug"))
        if self.debug:
            enable_logging_to_file()
        self.logger = get_logger(self.__class__.__name__, self.debug)
        self.logger.info("Config loaded: %s", self._redacted(self.config))

        self.notes_managers: Dict[str, NotesManager] = {}
        self.thread_pool = QThreadPool()
        self._worker: Optional[SyncWorker] = None
        self._is_auto_sync = True
        self._remove_obsolete_on_sync = False
        # Per-sync state, see `_reset_stats`
        self.synced_note_ids: Dict[str, Set[int]] = defaultdict(set)
        self.existing_note_ids: Dict[str, Set[int]] = {}
        #: Decks with a skipped/failed page: never prune them
        self._incomplete_decks: Set[str] = set()
        #: Decks whose notes may be pruned: page targets, and subdecks of
        #: pages synced with `subdecks`
        self._prune_decks: Set[str] = set()
        #: Target decks of pages synced with `subdecks`
        self._subdeck_roots: Set[str] = set()
        #: Config key of a token Notion rejected in this sync
        self._auth_failed: Optional[str] = None
        #: Token problems already reported this session (background syncs)
        self._auth_warned: Set[str] = set()
        #: page_id -> last_edited_time of pages synced in this run
        self._page_versions: Dict[str, Optional[int]] = {}
        self._sync_errors: List[str] = []
        self._processed = self._created = self._updated = self._deleted = 0

        self.notion_menu: Optional[QMenu] = None
        self._status_action: Optional[QAction] = None
        self.add_actions()
        main_window_did_init.append(self.auto_sync)
        profile_will_close.append(self.cancel_sync)
        self.timer = QTimer()
        self.timer.timeout.connect(self.auto_sync)
        self._apply_timer_interval()

    # -- config -----------------------------------------------------------

    @staticmethod
    def _redacted(config: Dict[str, Any]) -> Dict[str, Any]:
        """Copy of config with secrets masked, safe for logging."""
        return {
            k: ("***" if k in SECRET_KEYS and v else v)
            for k, v in config.items()
        }

    @staticmethod
    def _validate_config(config: Optional[Dict[str, Any]]) -> None:
        """:raises ValidationError: if config is invalid"""
        if not config:
            raise ValidationError("Config is empty")
        with open(BASE_DIR / "schemas/config_schema.json", encoding="utf8") as s:
            validate(config, json.load(s))

    def get_valid_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Return `config` if valid, else the add-on defaults."""
        try:
            self._validate_config(config)
        except ValidationError as exc:
            showCritical(str(exc), title=f"{ADDON_NAME}: config error")
            assert mw
            return cast(
                Dict[str, Any], mw.addonManager.addonConfigDefaults(str(BASE_DIR))
            )
        assert config
        return config

    def reload_config(self, new_config: Optional[Dict[str, Any]]) -> None:
        """Reload configuration (also called by Anki after the user edits it)."""
        if not new_config:
            assert mw
            new_config = mw.addonManager.getConfig(__name__)
        try:
            self._validate_config(new_config)
        except ValidationError as exc:
            self.logger.error("Config update error", exc_info=exc)
            showCritical(str(exc), title=f"{ADDON_NAME}: config error")
            return
        assert new_config
        self.config = new_config
        self._apply_timer_interval()

    def _apply_timer_interval(self) -> None:
        """(Re)start the auto-sync timer from `sync_every_minutes`."""
        minutes = self.config.get("sync_every_minutes", self.DEFAULT_SYNC_INTERVAL)
        interval_ms = int(minutes) * 60 * 1000
        if interval_ms <= 0:
            self.timer.stop()
        elif self.timer.interval() != interval_ms or not self.timer.isActive():
            self.timer.start(interval_ms)

    def get_notion_pages_config(self) -> List[PageJob]:
        """Configured pages. A page without `target_deck` uses its id."""
        jobs = []
        for spec in self.config.get("notion_pages", []):
            raw_id = spec["page_id"]
            jobs.append(PageJob(
                page_id=normalize_block_id(raw_id),
                deck=spec.get("target_deck") or raw_id,
                recursive=bool(spec.get("recursive", False)),
                subdecks=bool(spec.get("subdecks", False)),
            ))
        return jobs

    # -- persisted state --------------------------------------------------

    def _load_state(self) -> Dict[str, int]:
        try:
            return json.loads(STATE_FILE.read_text("utf8")).get("last_edited", {})
        except (OSError, ValueError):
            return {}

    def _save_state(self, versions: Dict[str, Optional[int]]) -> None:
        state = self._load_state()
        for page_id, edited in versions.items():
            if edited is None:
                state.pop(page_id, None)
            else:
                state[page_id] = edited
        try:
            USER_FILES_DIR.mkdir(exist_ok=True)
            STATE_FILE.write_text(json.dumps({"last_edited": state}), "utf8")
        except OSError as exc:
            self.logger.warning("Cannot save sync state: %s", exc)

    # -- menu / entry points ----------------------------------------------

    def add_actions(self) -> None:
        """Add Notion menu entry with actions to Tools menu."""
        assert mw
        self.notion_menu = mw.form.menuTools.addMenu(MENU_TITLE)
        self._status_action = QAction("Not synced yet", mw)
        self._status_action.setEnabled(False)
        self.notion_menu.addAction(self._status_action)
        self.notion_menu.addSeparator()
        load = QAction("Sync from Notion", mw)
        load.triggered.connect(lambda: self.sync())
        load_and_prune = QAction("Sync from Notion and remove deleted notes…", mw)
        load_and_prune.triggered.connect(self.sync_and_remove_obsolete)
        self.notion_menu.addActions((load, load_and_prune))

    def _set_menu_title(self, title: str) -> None:
        if self.notion_menu:
            self.notion_menu.setTitle(title)

    def _set_status(self, text: str) -> None:
        """Update the greyed-out "Last sync …" line at the top of the menu."""
        if self._status_action:
            self._status_action.setText(text)

    def auto_sync(self) -> None:
        """Background sync (timer / startup); silent unless debugging."""
        if self._worker:
            self.logger.info("Auto sync skipped: sync already in progress")
            return
        self.logger.info("Auto sync started")
        self.reload_config(None)
        self._is_auto_sync = True
        self._remove_obsolete_on_sync = False
        self._sync()

    def sync(self, remove_obsolete: bool = False) -> None:
        """Manual sync; always re-exports every page and reports the result.

        :param remove_obsolete: offer to delete notes missing from Notion
        """
        if self._worker:
            showInfo(
                "Sync is already in progress, please wait",
                title=ADDON_NAME,
            )
            return
        self.logger.info("Sync started (remove_obsolete=%s)", remove_obsolete)
        self.reload_config(None)
        self._is_auto_sync = False
        self._remove_obsolete_on_sync = remove_obsolete
        self._sync()

    def sync_and_remove_obsolete(self) -> None:
        """Perform synchronization and remove obsolete notes."""
        self.sync(remove_obsolete=True)

    def cancel_sync(self) -> None:
        """Stop a running sync (profile is closing)."""
        if self._worker:
            self.logger.info("Cancelling sync")
            self._worker.cancel()
            # Pages that never exported would look like deleted notes, so a
            # cancelled run must count as failed: no pruning, no saved page
            # versions. Stay silent, the profile is going away.
            self._sync_errors.append("Sync was cancelled")
            self._is_auto_sync = True

    # -- sync orchestration -----------------------------------------------

    def _manager(self, deck: str) -> NotesManager:
        """NotesManager for a deck, created on demand."""
        assert mw and mw.col
        nm = self.notes_managers.get(deck)
        if nm is None or nm.collection is not mw.col:
            nm = self.notes_managers[deck] = NotesManager(
                collection=mw.col, deck_name=deck, debug=self.debug
            )
        return nm

    def _ensure_notes_managers(self, jobs: List[PageJob]) -> None:
        """Create managers for every page's deck and its existing subdecks.

        Subdecks are always searched, so a card is found even after it was
        moved into a subdeck. They may only be pruned for pages synced with
        `subdecks`, whose subdecks this add-on owns.
        """
        assert mw and mw.col
        self._prune_decks = set()
        self._subdeck_roots = {job.deck for job in jobs if job.subdecks}
        # Drop managers left over from a previous run or profile
        self.notes_managers = {
            deck: nm for deck, nm in self.notes_managers.items()
            if nm.collection is mw.col
        }
        for job in jobs:
            self._manager(job.deck)
            self._prune_decks.add(job.deck)
            root_id = mw.col.decks.id(job.deck, create=True)
            for deck_id in mw.col.decks.deck_and_child_ids(root_id):
                name = mw.col.decks.name(deck_id)
                self._manager(name)
                if job.subdecks:
                    self._prune_decks.add(name)

    def _family(self, root: str) -> List[NotesManager]:
        """Managers of a page's target deck and all its subdecks."""
        return [
            nm for deck, nm in self.notes_managers.items()
            if deck == root or deck.startswith(root + "::")
        ]

    def _reset_stats(self) -> None:
        """Snapshot existing notes and reset per-sync counters."""
        self.synced_note_ids = defaultdict(set)
        self.existing_note_ids = {}
        for deck, nm in self.notes_managers.items():
            nm.reset_index()
            self.existing_note_ids[deck] = nm.existing_note_ids
        self._incomplete_decks = set()
        self._page_versions = {}
        self._sync_errors = []
        self._auth_failed = None
        self._processed = self._created = self._updated = self._deleted = 0

    def _sync(self) -> None:
        """Start a sync in the background."""
        assert mw
        if not mw.col:
            self.logger.error("Collection is not loaded")
            return
        jobs = self.get_notion_pages_config()
        if not jobs:
            self.logger.info("No pages configured")
            return
        try:
            self._ensure_notes_managers(jobs)
            self._reset_stats()
        except Exception as exc:
            self.logger.exception("Cannot prepare sync")
            if not self._is_auto_sync:
                showCritical(str(exc), title=f"{ADDON_NAME}: sync error")
            return

        # Skipping unchanged pages is for background syncs only: a manual
        # sync is what the user reaches for when something looks stale
        skip_unchanged = (
            self._is_auto_sync and self.config.get("skip_unchanged_pages", False)
        )
        worker = SyncWorker(
            jobs=jobs,
            notion_token=self.config["notion_token"],
            file_token=self.config.get("file_token", ""),
            notion_namespace=self.config.get("notion_namespace", ""),
            timezone=self.config.get("timezone") or local_timezone(),
            max_in_flight=1 if self.config.get("sequential_sync") else len(jobs),
            stagger=float(self.config.get("worker_stagger_delay", 1)),
            known_last_edited=self._load_state() if skip_unchanged else None,
            debug=self.debug,
        )
        worker.signals.result.connect(self.handle_worker_result)
        worker.signals.error.connect(self.handle_worker_error)
        worker.signals.page_skipped.connect(self.handle_page_skipped)
        worker.signals.page_done.connect(self.handle_page_done)
        worker.signals.auth_failed.connect(self.handle_auth_failed)
        worker.signals.finished.connect(self.handle_sync_finished)
        self._worker = worker
        self._set_menu_title(f"{MENU_TITLE} (syncing…)")
        self.thread_pool.start(worker)

    # -- worker signal handlers (main thread) -----------------------------

    def handle_worker_result(
        self, deck: str, root: str, notes: List[AnkiNote]
    ) -> None:
        """Write the notes for one deck to the collection.

        :param deck: deck the notes belong in
        :param root: target deck of the page they came from
        :param notes: notes
        """
        assert mw
        if not mw.col or root not in self.notes_managers or (
            self.notes_managers[root].collection is not mw.col
        ):
            self.logger.warning("Dropping results for %s: collection changed", deck)
            self._incomplete_decks.add(deck)
            return
        nm = self._manager(deck)
        for note in notes:
            if not note.front:
                self.logger.warning("Note front is empty: %s", note)
                continue
            self._processed += 1
            try:
                owner, note_id = nm, nm.find_note(note)
                if not note_id:
                    # Maybe it lives in another deck of this page's family
                    for other in self._family(root):
                        if other is not nm and (found := other.find_note(note)):
                            owner, note_id = other, found
                            break
                if note_id and owner is not nm and root in self._subdeck_roots:
                    # The subpage structure decides the deck
                    nm.adopt(note_id, owner)
                    owner = nm
                if note_id:
                    # A new id is returned if the note had to be recreated
                    # with another note type
                    is_updated, note_id = owner.update_note(note_id, note)
                    self._updated += is_updated
                else:
                    note_id = nm.create_note(note)
                    self._created += 1
                self.synced_note_ids[owner.deck_name].add(note_id)
            except Exception as exc:
                msg = f"Error processing note in deck {deck}: {exc}"
                self.logger.exception(msg)
                self._sync_errors.append(msg)
                self._incomplete_decks.add(deck)

    def handle_worker_error(self, message: str) -> None:
        self.logger.error("Worker error: %s", message)
        self._sync_errors.append(message)

    def handle_page_skipped(self, page_id: str, deck: str) -> None:
        # Its notes were not seen in this run, so neither the deck nor its
        # subdecks may be pruned
        self._incomplete_decks.add(deck)
        self._incomplete_decks.update(
            d for d in self.notes_managers if d.startswith(deck + "::")
        )

    def handle_auth_failed(self, token_key: str) -> None:
        self._auth_failed = token_key
        self._sync_errors.append(f"Notion rejected your {token_key}")

    def handle_page_done(self, page_id: str, last_edited: Optional[int]) -> None:
        self._page_versions[page_id] = last_edited

    def handle_sync_finished(self) -> None:
        """Report, optionally prune obsolete notes and persist page versions."""
        self._worker = None
        self._set_menu_title(MENU_TITLE)
        now = datetime.now().strftime("%H:%M")
        try:
            if self._page_versions and not self._sync_errors:
                self._save_state(self._page_versions)
            if self._auth_failed:
                self._set_status(f"Last sync {now}: {self._auth_failed} expired")
                self._report_auth_failure(self._auth_failed)
                return
            if self._sync_errors:
                self._set_status(f"Last sync {now}: failed, see log")
                if not self._is_auto_sync:
                    showCritical(
                        "\n".join(self._sync_errors),
                        title=f"{ADDON_NAME}: Notion sync failed",
                    )
                return
            if self._remove_obsolete_on_sync:
                self._remove_obsolete()
            self._set_status(
                f"Last sync {now}: {self._created} new, {self._updated} updated"
                + (f", {self._deleted} removed" if self._deleted else "")
            )
            if not self._is_auto_sync:
                showInfo(
                    "Notion sync finished:\n"
                    f"Processed: {self._processed}\n"
                    f"Created: {self._created}\n"
                    f"Updated: {self._updated}\n"
                    f"Removed: {self._deleted}",
                    title=ADDON_NAME,
                )
        except Exception as exc:
            self.logger.exception("Error finishing sync")
            if not self._is_auto_sync:
                showCritical(f"Sync failed with error: {exc}", title=f"{ADDON_NAME}: sync error")
        finally:
            self.logger.info(
                "Sync finished: processed=%s created=%s updated=%s deleted=%s "
                "errors=%s",
                self._processed, self._created, self._updated, self._deleted,
                len(self._sync_errors),
            )
            self._remove_obsolete_on_sync = False

    def _report_auth_failure(self, token_key: str) -> None:
        """Explain how to renew a rejected token.

        Manual syncs always show it; background syncs once per session, so
        an expired token doesn't pop up a dialog every few minutes.
        """
        if self._is_auto_sync:
            if token_key in self._auth_warned:
                return
            self._auth_warned.add(token_key)
        showCritical(TOKEN_HELP[token_key], title=f"{ADDON_NAME}: login expired")

    def _remove_obsolete(self) -> None:
        """Ask, then suspend (or delete) notes whose toggle is gone."""
        assert mw
        # A note synced into any deck this run is not obsolete (it may have
        # moved between subdecks)
        synced = set().union(*self.synced_note_ids.values())
        to_remove: Dict[str, Dict[int, str]] = {}
        for deck, ids in self.existing_note_ids.items():
            if deck not in self._prune_decks or deck in self._incomplete_decks:
                continue
            nm = self.notes_managers[deck]
            candidates = ids - synced
            # Already suspended as deleted in an earlier run
            candidates -= nm.tagged(candidates, REMOVED_TAG)
            # `fronts` only returns notes that still exist
            fronts = nm.fronts(candidates)
            if fronts:
                to_remove[deck] = fronts
        total = sum(len(f) for f in to_remove.values())
        if not total:
            return

        delete = self.config.get("remove_mode", "suspend") == "delete"
        listed = [
            f"{deck}:  {front[:100] or '(empty)'}"
            for deck, fronts in to_remove.items()
            for front in sorted(fronts.values())
        ]
        if len(listed) > MAX_LISTED_REMOVALS:
            more = len(listed) - MAX_LISTED_REMOVALS
            listed = listed[:MAX_LISTED_REMOVALS] + [f"… and {more} more"]
        box = QMessageBox(mw)
        box.setWindowTitle(ADDON_NAME)
        box.setIcon(QMessageBox.Icon.Warning)
        if delete:
            box.setText(
                f"{total} note(s) no longer exist in Notion.\n\n"
                "Delete them? Their review history is deleted too and "
                "this cannot be undone."
            )
        else:
            box.setText(
                f"{total} note(s) no longer exist in Notion.\n\n"
                f"Suspend their cards and tag them \"{REMOVED_TAG}\"? Nothing "
                "is deleted: unsuspend them in the Browser to keep them, or "
                "search tag:" + REMOVED_TAG + " to delete them yourself."
            )
        box.setInformativeText("Click “Show Details…” to see which ones.")
        box.setDetailedText("\n".join(listed))
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.exec()
        # exec() returns a plain int in PyQt6; compare the clicked button
        if box.standardButton(box.clickedButton()) != QMessageBox.StandardButton.Yes:
            return
        for deck, fronts in to_remove.items():
            if delete:
                self.notes_managers[deck].remove_notes(set(fronts))
            else:
                self.notes_managers[deck].suspend_notes(set(fronts), REMOVED_TAG)
        self._deleted += total


NotionSyncPlugin()
