"""Notes manager."""

from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from anki.consts import MODEL_CLOZE
from anki.models import ModelManager, ModelsDictProxy
from anki.notes import Note
from anki.utils import ids2str

from .helpers import get_logger, normalize_front, safe_str
from .parser import AnkiNote



#: `#<block id>` anchor at the end of a note's Notion link
BLOCK_ANCHOR_RE = re.compile(r"#([0-9a-f]{32})\b")
#: Tag on notes whose toggle was deleted in Notion (cards are suspended)
REMOVED_TAG = "notion-deleted"


class NoteManagerException(Exception):
    """Note manager exception."""


class NotesManager:
    """Notes manager.

    Creates deck and models, adds and removes notes.
    """

    #: Note model stylesheet
    MODEL_CSS: str = (Path(__file__).parent / "model.css").read_text("utf-8")
    #: Note type names. Keep these as they are: existing cards are matched
    #: by them, and renaming would detach every card from the add-on.
    #: Note model name
    MODEL_NAME: str = "notion-anki-sync"
    #: Cloze note model name
    CLOZE_MODEL_NAME: str = "notion-anki-sync-cloze"
    #: Card template name
    CARD_TEMPLATE_NAME: str = "Question-Answer"
    #: Cloze card template name
    CLOZE_CARD_TEMPLATE_NAME: str = "Cloze"
    #: Note front side template
    FRONT_TMPL: str = '<div class="front">{{Front}}</div>'
    #: Note back side template
    BACK_TMPL: str = (
        '<div class="front-on-back">{{Front}}</div>'
        '<hr id="answer">'
        '<div class="back">{{Back}}</div>'
        '<hr id="source">'
        '<div class="backlink">{{Source}}</div>'
    )
    #: Cloze note front side template
    CLOZE_FRONT_TMPL: str = '<div class="front">{{cloze:Front}}</div>'
    #: Cloze note back side template
    CLOZE_BACK_TMPL: str = (
        '<div class="front">{{cloze:Front}}</div>'
        '<hr id="source">'
        '<div class="backlink">{{Source}}</div>'
    )

    def __init__(self, collection, deck_name: str, debug: bool = False) -> None:
        """Init syncer.

        :param collection: Anki collection
        :param deck_name: name of the target deck
        :param debug: debug mode
        """
        self.logger = get_logger(self.__class__.__name__, debug)
        self.collection = collection
        self.deck_name = deck_name
        self.deck = self.get_deck()
        self.create_models()
        # Built lazily on first find_note call, reset at the start of a sync
        self._by_block_id: Optional[Dict[str, int]] = None
        self._legacy_fronts: Optional[Dict[str, int]] = None
        self._all_fronts: Optional[Dict[str, int]] = None

    def _models(self) -> List[Dict[str, Any]]:
        """This add-on's own note types."""
        return [
            m for m in (
                self.collection.models.by_name(self.MODEL_NAME),
                self.collection.models.by_name(self.CLOZE_MODEL_NAME),
            )
            if m
        ]

    def _model_ids(self) -> List[int]:
        """Ids of this add-on's own note types."""
        return [m["id"] for m in self._models()]

    @property
    def existing_note_ids(self) -> Set[int]:
        """Ids of this add-on's notes in exactly this deck.

        Only our own note types count, and subdecks are excluded: a page
        synced into `Daily` must never claim (and so never prune) notes in
        `Daily::Something` or notes the user made by hand.

        :returns: existing note ids
        """
        model_ids = self._model_ids()
        if not model_ids:
            return set()
        return set(self.collection.db.list(
            "select distinct n.id from notes n join cards c on c.nid = n.id "
            f"where c.did = ? and n.mid in {ids2str(model_ids)}",
            self.deck,
        ))

    def _build_index(self) -> None:
        """Index this deck's notes by Notion block id and by question text.

        A note's block id is the `#<id>` anchor of its Source link. Notes
        synced before block ids were recorded have none; they are matched
        by question text once and get their id on that update.
        """
        if self._by_block_id is not None:
            return
        self._by_block_id, self._legacy_fronts, self._all_fronts = {}, {}, {}
        note_ids = self.existing_note_ids
        if not note_ids:
            return
        source_ord = {
            m["id"]: next(f["ord"] for f in m["flds"] if f["name"] == "Source")
            for m in self._models()
        }
        # One SQL query instead of a get_note() round-trip per note.
        rows = self.collection.db.all(
            f"select id, mid, flds from notes where id in {ids2str(note_ids)}"
        )
        for note_id, mid, flds in rows:
            values = flds.split("\x1f")
            front = self._normalize_front(values[0])  # Front is field 0
            self._all_fronts[front] = note_id
            block_id = self._block_id_of(values[source_ord[mid]])
            if block_id:
                self._by_block_id[block_id] = note_id
            else:
                self._legacy_fronts[front] = note_id

    @staticmethod
    def _block_id_of(source_field: str) -> Optional[str]:
        match = BLOCK_ANCHOR_RE.search(source_field or "")
        return match.group(1) if match else None

    def reset_index(self) -> None:
        """Drop the cached indexes so the next sync re-reads the deck.

        Without this, notes deleted or edited in Anki between syncs would
        still be found under their stale id.
        """
        self._by_block_id = self._legacy_fronts = self._all_fronts = None

    def _remember(self, note_id: int, note: AnkiNote) -> None:
        """Record a note created/updated in this sync in the indexes."""
        if self._by_block_id is None:
            return
        front = self._normalize_front(note.front)
        self._all_fronts[front] = note_id
        if note.block_id:
            self._by_block_id[note.block_id] = note_id
            # It now has an id: never match it by question text again
            if self._legacy_fronts.get(front) == note_id:
                del self._legacy_fronts[front]
        else:
            self._legacy_fronts[front] = note_id

    def forget(self, note_id: int) -> None:
        """Drop a note from the indexes (it moved to another deck)."""
        if self._by_block_id is None:
            return
        for index in (self._by_block_id, self._legacy_fronts, self._all_fronts):
            for key in [k for k, v in index.items() if v == note_id]:
                del index[key]

    #: Shared with tests: see helpers.normalize_front
    _normalize_front = staticmethod(normalize_front)

    def create_models(self) -> None:
        """Create Question-Answer and Cloze models.

        :raises NoteManagerException: if cannot find cloze model to copy
        """
        model_manager: ModelManager = self.collection.models
        models_updated = False
        
        # Create new model if not exists
        model = model_manager.by_name(self.MODEL_NAME)
        if not model:
            model = model_manager.new(self.MODEL_NAME)
            # Add fields
            for field_name in ("Front", "Back", "Source"):
                field = model_manager.new_field(field_name)
                model_manager.add_field(model, field)
            # Add template
            template = model_manager.new_template(self.CARD_TEMPLATE_NAME)
            model_manager.add_template(model, template)
            models_updated = True
            self.logger.info("Model created")
        else:
            template = model["tmpls"][0]
        
        # Update template and CSS only if needed
        needs_update = (
            template["qfmt"] != self.FRONT_TMPL or
            template["afmt"] != self.BACK_TMPL or
            model["css"] != self.MODEL_CSS
        )
        
        if needs_update or models_updated:
            template["qfmt"] = self.FRONT_TMPL
            template["afmt"] = self.BACK_TMPL
            model["css"] = self.MODEL_CSS
            model_manager.save(model)
            self.logger.info("Model updated")

        # Create cloze model if not exists
        cloze_model = model_manager.by_name(self.CLOZE_MODEL_NAME)
        cloze_models_updated = False
        
        if not cloze_model:
            cloze_model = model_manager.new(self.CLOZE_MODEL_NAME)
            cloze_model["type"] = MODEL_CLOZE

            # Add fields
            for field_name in ("Front", "Source"):
                field = model_manager.new_field(field_name)
                model_manager.add_field(cloze_model, field)
            # Add template
            cloze_template = model_manager.new_template(self.CLOZE_CARD_TEMPLATE_NAME)
            model_manager.add_template(cloze_model, cloze_template)
            cloze_models_updated = True
            self.logger.info("CLOZE_Model created")
        else:
            cloze_template = cloze_model["tmpls"][0]
        
        # Update cloze template and CSS only if needed
        cloze_needs_update = (
            cloze_template["qfmt"] != self.CLOZE_FRONT_TMPL or
            cloze_template["afmt"] != self.CLOZE_BACK_TMPL or
            cloze_model["css"] != self.MODEL_CSS
        )
        
        if cloze_needs_update or cloze_models_updated:
            cloze_model["css"] = self.MODEL_CSS
            cloze_template["qfmt"] = self.CLOZE_FRONT_TMPL
            cloze_template["afmt"] = self.CLOZE_BACK_TMPL
            model_manager.save(cloze_model)
            self.logger.info("Cloze model updated")

    def get_deck(self) -> int:
        """Get or create target deck.

        :returns: working deck
        """
        deck_id = self.collection.decks.id(self.deck_name, create=True)
        assert deck_id  # mypy
        return deck_id

    def find_note(self, note: AnkiNote) -> Optional[int]:
        """Find the existing note for a toggle in this deck.

        By Notion block id first, so rewording a question keeps the card and
        its review history. Notes without a recorded id fall back to their
        question text.

        :param note: note
        :returns: note id if found else None
        """
        self._build_index()
        front = self._normalize_front(note.front)
        if note.block_id:
            note_id = self._by_block_id.get(note.block_id)
            if note_id is None:
                note_id = self._legacy_fronts.get(front)
        else:
            note_id = self._all_fronts.get(front)
        self.logger.debug("find_note front=%s -> id=%s", safe_str(note.front), note_id)
        return note_id

    def adopt(self, note_id: int, previous: "NotesManager") -> None:
        """Move a note's cards from another managed deck into this one."""
        card_ids = self.collection.db.list(
            "select id from cards where nid = ?", note_id
        )
        self.collection.set_deck(card_ids, self.deck)
        previous.forget(note_id)
        self.logger.info(
            "Moved note %s: %s -> %s", note_id, previous.deck_name, self.deck_name
        )

    def fronts(self, note_ids: Set[int]) -> Dict[int, str]:
        """Plain-text questions of the given notes (for confirmation dialogs)."""
        if not note_ids:
            return {}
        rows = self.collection.db.all(
            f"select id, flds from notes where id in {ids2str(note_ids)}"
        )
        return {
            nid: self._normalize_front(flds.split("\x1f", 1)[0])
            for nid, flds in rows
        }

    def tagged(self, note_ids: Set[int], tag: str) -> Set[int]:
        """Those of `note_ids` that carry `tag`."""
        if not note_ids:
            return set()
        return set(self.collection.find_notes(f"tag:{tag}")) & set(note_ids)

    def suspend_notes(self, note_ids: Set[int], tag: str) -> None:
        """Suspend all cards of the notes and tag them."""
        card_ids = self.collection.db.list(
            f"select id from cards where nid in {ids2str(note_ids)}"
        )
        self.collection.sched.suspend_cards(card_ids)
        self.collection.tags.bulk_add(list(note_ids), tag)
        self.logger.info("Suspended notes: %s", note_ids)

    def _fill_fields(
        self, target: Note, source: AnkiNote, model: ModelsDictProxy
    ) -> List[Dict[str, str]]:
        """Fill Anki note fields from the `AnkiNote` instance.

        :param target: target Anki note
        :param source: source Anki note model
        :param model: model
        :returns: updated data
        """
        updated_data = []
        for field in model["flds"]:
            field_name = field["name"]
            attr_name = field_name.lower()
            if not hasattr(source, attr_name):
                continue
            new_value = getattr(source, attr_name)
            existing_value = target[field_name]
            if existing_value != new_value:
                updated_data.append(
                    {
                        f"{field_name}_old": existing_value,
                        f"{field_name}_new": new_value,
                    }
                )
                target[field_name] = new_value
        return updated_data

    def create_note(self, note: AnkiNote) -> int:
        """Create new note.

        :param note: note
        :returns: id of the note created
        """
        # Pick the model
        if not note.back:
            model = self.collection.models.by_name(self.CLOZE_MODEL_NAME)
        else:
            model = self.collection.models.by_name(self.MODEL_NAME)
        # Create a note
        anki_note = Note(self.collection, model)
        # Upload note media (may rename files referenced in back)
        media_manager = self.collection.media
        for image in note.images:
            # Skip if file already exists
            if media_manager.have(image.filename):
                continue
            maybe_new_filename = media_manager.write_data(image.filename, image.data)
            self.logger.info("Image stored: %s", image.filename)
            if maybe_new_filename:
                self.logger.debug(
                    "Renaming image %s -> %s",
                    image.filename,
                    maybe_new_filename,
                )
                if note.back:
                    note.back = note.back.replace(image.filename, maybe_new_filename)
        # Fill fields BEFORE add_note to ensure card generation
        self._fill_fields(anki_note, note, model)
        anki_note.tags = list(note.tags)
        # Add to deck
        deck_id = self.get_deck()
        self.collection.add_note(anki_note, deck_id)
        note_id = anki_note.id
        self.logger.info("Note created: id=%s, front=%s", note_id, safe_str(note.front))
        # Keep the indexes in sync so later toggles in this sync can't match
        # the same note again
        self._remember(note_id, note)
        return note_id

    def update_note(self, note_id: int, note: AnkiNote) -> Tuple[bool, int]:
        """Update existing note.

        :param note_id: id of a note to update
        :param note: note
        :returns: (whether the note has been updated, current note id). The
            id changes when the note had to be recreated with another type.
        """
        updated_data = []
        # Pick right model
        if not note.back:
            model = self.collection.models.by_name(self.CLOZE_MODEL_NAME)
        else:
            model = self.collection.models.by_name(self.MODEL_NAME)
        # Get an existing note
        existing_note = self.collection.get_note(note_id)
        # Ensure note is of right model
        if existing_note.mid != model["id"]:
            self.logger.warning(
                "Note type changed: note_id=%s, old=%s, new=%s",
                note_id,
                existing_note.mid,
                model["id"],
            )
            # It's easier to remove the note and create a new one...
            self.collection.remove_notes([note_id])
            note_id = self.create_note(note)
            updated_data.append(
                {"model_old": existing_note.mid, "model_new": model["id"]}
            )
        else:
            # A toggle that came back after its card was suspended as
            # deleted: bring the card back
            if REMOVED_TAG in existing_note.tags:
                self.collection.sched.unsuspend_cards(existing_note.card_ids())
                updated_data.append({"restored": True})
            # Update tags
            if set(existing_note.tags) != set(note.tags):
                updated_data.append(
                    {"tags_old": existing_note.tags, "tags_new": note.tags}
                )
                existing_note.tags = note.tags
            # Update field values
            updated_data += self._fill_fields(existing_note, note, model)
            # Upload note media
            media_manager = self.collection.media
            for image in note.images:
                # Skip if file already exists
                if media_manager.have(image.filename):
                    continue
                maybe_new_filename = media_manager.write_data(
                    image.filename, image.data
                )
                self.logger.info("Image stored: %s", image.filename)
                if maybe_new_filename:
                    self.logger.debug(
                        "Renaming image %s -> %s",
                        image.filename,
                        maybe_new_filename,
                    )
                    existing_note["Back"] = existing_note["Back"].replace(
                        image.filename, maybe_new_filename
                    )
                    updated_data.append(
                        {
                            "filename_old": image.filename,
                            "filename_new": maybe_new_filename,
                        }
                    )
            if updated_data:
                self.collection.update_note(existing_note)
                self.logger.info(
                    "Note updated: note_id=%s, data=%s", note_id, updated_data
                )
            else:
                self.logger.debug("No changes in note: note_id=%s", note_id)
            self._remember(note_id, note)
        return bool(updated_data), note_id

    def remove_notes(self, note_ids: Set[int]) -> None:
        """Remove notes with given IDs.

        :param note_ids: note ids to remove
        """
        self.logger.info("Removing notes: %s", note_ids)
        self.collection.remove_notes(list(note_ids))
