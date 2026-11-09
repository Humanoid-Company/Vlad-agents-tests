"""Function schemas the GPT-Live Responses backend may call. Add new tools here and register their
handlers on the ToolExecutor (server/web_users.py, assistant.py)."""
from __future__ import annotations

from voice.options import LANGUAGE_OPTIONS, LIVE_VOICE_OPTIONS, SPEED_OPTIONS, STYLE_OPTIONS, VOICE_PERSONAS


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    params: dict = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        params["required"] = required
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": params,
    }


LIVE_BACKEND_TOOLS: list[dict] = [
    _fn(
        "set_assistant_name",
        "Change the assistant's spoken name.",
        {"name": {"type": "string"}},
        required=["name"],
    ),
    _fn(
        "set_voice_style",
        "Change how the current voice speaks, applied right away: speed (slow/normal/fast) and "
        "style (calm/normal/expressive). For «говори повільніше / швидше / спокійніше / "
        "емоційніше». Omitted fields keep their value.",
        {
            "speed": {"type": "string", "enum": list(SPEED_OPTIONS)},
            "style": {"type": "string", "enum": list(STYLE_OPTIONS)},
        },
    ),
    _fn(
        "change_language",
        "Change conversation language immediately.",
        {"language": {"type": "string", "enum": list(LANGUAGE_OPTIONS)}},
        required=["language"],
    ),
    _fn(
        "web_search",
        "Search the public web for current or externally verifiable information. "
        "Use for recent events, news, current facts, software versions, companies, "
        "products, technologies, or information that may have changed after the model's "
        "knowledge cutoff. Do not use for translation, summarization, writing, "
        "or questions answerable without external lookup. "
        "recency_days limits results to roughly the last N days (e.g. 1 for «сьогодні» / latest news).",
        {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {
                "type": "integer",
                "description": "How many results to return (default 5, hard max 10)",
            },
            "recency_days": {
                "type": "integer",
                "description": "Prefer results from the last N days (optional)",
            },
        },
        required=["query"],
    ),
    _fn(
        "end_conversation",
        "End the conversation and return to sleep. Call silently, and only when the user clearly says goodbye or "
        "asks to end it — never after finishing a task, a welcome or a refusal, on silence or noise, or because "
        "they said thanks.",
        {},
    ),
]

# Web only (the page sends camera frames; the desktop has no camera yet): server/app.py adds it.
LOOK_AT_CAMERA_TOOL: dict = _fn(
    "look_at_camera",
    "Look through the user's camera: returns what a frame taken right now shows (as text, or the image "
    "itself). Use for anything about "
    "what is visible — «подивись на мене», «що в мене в руці», «що тут написано», «як я виглядаю», "
    "«що це за рослина». Then tell what the look actually saw that answers the question. "
    "If the result says the camera is off, pass that on.",
    {"question": {"type": "string", "description": "What the user wants to know about the view"}},
    required=["question"],
)

# Web only: the team's uploaded knowledge (server/knowledge.py, the page «Знання»).
KNOWLEDGE_LOOKUP_TOOL: dict = _fn(
    "knowledge_lookup",
    "Look up the team's uploaded knowledge: records about people (age, hobbies, favourite films, role…) "
    "and events. Use for any question about a person or event from it — «що любить Олег», «скільки років "
    "Марині», «хто з них фанат Тарантіно», «що подарувати Ірині». Returns the matching records (all of "
    "them while they fit); answer only from them.",
    {
        "query": {"type": "string", "description": "The name(s) or words as heard, e.g. «Олег хобі» or «Тарантіно»"},
        "set": {"type": "string", "description": "A set's name if the user named one («корпоратив»), else omit"},
    },
    required=["query"],
)

# Web: Єва writes down what she is told about a person (server/knowledge.py remember()) and recalls it with
# knowledge_lookup. Not for the concierge.
REMEMBER_PERSON_TOOL: dict = _fn(
    "remember_person",
    "Write down facts the user told you about a person, to remember them in later talks — «запам'ятай, що "
    "Олег любить Тарантіно», «це моя сестра Іра, їй 30». A person already written down gets the new facts "
    "added. Only what the user said and wants kept; never passwords, bank data or health details.",
    {
        "name": {"type": "string", "description": "The person's name as the user said it, with the surname if given"},
        "facts": {
            "type": "object",
            "description": "Facts as short label → value, e.g. {\"Хобі\": \"шахи\", \"Вік\": \"30\", \"Хто це\": \"сестра користувача\"}",
            "additionalProperties": {"type": "string"},
        },
    },
    required=["name", "facts"],
)

# Web «Консьєрж» mode: the backend marks who the guest is once the check question confirmed it.
CONFIRM_GUEST_TOOL: dict = _fn(
    "confirm_guest",
    "Concierge: record the outcome of checking a guest against the team's list — only after they answered the "
    "check question. verified=true only when that answer matches their record; false after a second miss. The "
    "server refuses verified=true without their own answer (not their name), and says if they were already let in.",
    {
        "full_name": {"type": "string", "description": "The person's full name as in the records"},
        "verified": {"type": "boolean"},
        "guest_answer": {"type": "string", "description": "The guest's own words in answer to the check question"},
    },
    required=["full_name", "verified", "guest_answer"],
)

# Web «Консьєрж»: who on the list a name belongs to — matched by the server (short forms, case endings, near
# spellings), not guessed by the model.
FIND_GUEST_TOOL: dict = _fn(
    "find_guest",
    "Concierge: check a name a person gave against the team's guest list. The server matches short forms "
    "(«Катя» → Катерина), case endings and near spellings, and says what to reply: not on the list, ask the "
    "surname, already registered, or the one person with the fact to ask the check question about. It "
    "searches by first name, by surname and by the full name (with the patronymic too), in any order.",
    {
        "name": {"type": "string", "description": "The first name as heard — or the full name as heard, whole, "
                 "in the order they said it"},
        "surname": {"type": "string", "description": "The surname (or patronymic) if they said it separately, else omit"},
    },
    required=["name"],
)
