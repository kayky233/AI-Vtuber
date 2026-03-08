from __future__ import annotations

import random
import re
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable


DEFAULT_FALLBACK_RESPONSES = (
    "这个问题我还没学会，你可以换个说法试试。",
    "我暂时答不上来，教教我这个问题的标准回复吧。",
    "这个我还不会，你可以先用 train.py 给我补充语料。",
)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _score_similarity(prompt: str, question: str) -> float:
    if prompt == question:
        return 1.0
    if prompt in question or question in prompt:
        return 0.85
    return SequenceMatcher(None, prompt, question).ratio()


@dataclass(frozen=True)
class BotResponse:
    text: str

    def __str__(self) -> str:
        return self.text


def initialize_database(database_path: str | Path) -> None:
    db_path = Path(database_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_pairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                answer TEXT NOT NULL
            )
            """
        )


def load_pairs(database_path: str | Path) -> list[tuple[str, str]]:
    db_path = Path(database_path)
    initialize_database(db_path)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT question, answer FROM conversation_pairs ORDER BY id"
        )
        return list(rows.fetchall())


def replace_pairs(database_path: str | Path, pairs: Iterable[tuple[str, str]]) -> int:
    db_path = Path(database_path)
    pair_list = list(pairs)
    initialize_database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM conversation_pairs")
        connection.executemany(
            "INSERT INTO conversation_pairs (question, answer) VALUES (?, ?)",
            pair_list,
        )
    return len(pair_list)


class SimpleChatBot:
    def __init__(
        self,
        name: str,
        database_path: str = "db.sqlite3",
        fallback_responses: Iterable[str] | None = None,
    ) -> None:
        self.name = name
        self.database_path = Path(database_path)
        self.fallback_responses = tuple(fallback_responses or DEFAULT_FALLBACK_RESPONSES)
        self._pairs = load_pairs(self.database_path)

    def reload(self) -> None:
        self._pairs = load_pairs(self.database_path)

    def find_best_response(self, prompt: str) -> tuple[BotResponse | None, float]:
        normalized_prompt = _normalize_text(prompt)
        if not normalized_prompt:
            return None, 0.0

        best_score = 0.0
        best_answer = None
        for question, answer in self._pairs:
            score = _score_similarity(normalized_prompt, _normalize_text(question))
            if score > best_score:
                best_score = score
                best_answer = answer

        if best_answer:
            return BotResponse(best_answer), best_score
        return None, 0.0

    def get_response(self, prompt: str) -> BotResponse:
        best_response, best_score = self.find_best_response(prompt)
        if best_response is not None and best_score >= 0.55:
            return best_response
        return BotResponse(random.choice(self.fallback_responses))
