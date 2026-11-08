"""Chunking profiles (manifest `chunking_profile`). See app/ingestion/chunking.py."""

from app.ingestion.chunkers.form import chunk_form
from app.ingestion.chunkers.question_headings import chunk_question_headings
from app.ingestion.chunkers.rbi_directions import chunk_rbi_directions
from app.ingestion.chunkers.rbi_faq import chunk_rbi_faq
from app.ingestion.chunking import Profile
from app.schemas.taxonomy import ChunkingProfile

PROFILES: dict[ChunkingProfile, Profile] = {
    ChunkingProfile.RBI_DIRECTIONS: chunk_rbi_directions,
    ChunkingProfile.RBI_FAQ: chunk_rbi_faq,
    ChunkingProfile.QUESTION_HEADINGS: chunk_question_headings,
    ChunkingProfile.FORM: chunk_form,
}
