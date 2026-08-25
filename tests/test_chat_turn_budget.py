"""How a chat turn's context budget is derived from the model actually in use.

The motivating measurement, from `sbpeye_debug.db` over 2026-08-15..25 — 135 chat
provider requests, the largest at **285,007** prompt tokens on a single call and
1,096,829 summed across one turn. Two independent causes, both of them a number that
did not know what model it was talking to:

* `AIConfig.for_user` never set `max_context_tokens`, so every chat turn ran on the
  dataclass default of 4,000 — which is not any model's window. `AIConfig.from_db`
  meanwhile held 1,310,720, detected from the provider and read by nothing in chat.
  The one turn that *did* run on the raw window built a 927,622-character selected
  circular context out of it.
* `search_corpus` handed back a fixed 40,000 + 24,000 + 8,000 characters however small
  the window was — a measured ~75,000 characters per call, which is 2.3x the whole
  window of an 8k local model, five times over in a turn that searches five times.

The requests grew monotonically with the tool rounds: iteration 1 median 9,487
characters, iteration 5 median 419,799, because the loop keeps every result. So the
budget is now divided between the turn's contributors — the selected-circular context
plus one per tool round — and "the turn fits" is arithmetic rather than hope.
"""

from types import SimpleNamespace

import pytest

from sbpeye import ai
from sbpeye.ai import (
    LAW_SEARCH_PASSAGE_BUDGET_CHARS,
    SEARCH_INLINE_BODY_BUDGET_CHARS,
    SEARCH_PASSAGE_BUDGET_CHARS,
    AIClient,
    AIConfig,
    get_ai_client_for_user,
)
from sbpeye.models import User

from conftest import make_circular


# Windows spanning what the deployment actually meets: an 8k local model, the
# per-provider fallbacks, a mid-size hosted model, and the 1,310,720 OpenRouter
# reports for the chat model in use.
WINDOWS = [8_192, 16_384, 32_768, 65_536, 131_072, 200_000, 1_310_720]


@pytest.fixture(autouse=True)
def _clear_window_cache():
    """`_window_cache` is process-wide, so one test's probe must not answer another's."""
    ai._window_cache.clear()
    yield
    ai._window_cache.clear()


def _client(window: int | None, provider: str = "openrouter") -> AIClient:
    client = AIClient(AIConfig(provider=provider, api_key="test", model="test"))
    client.detect_context_window = lambda: window  # type: ignore[method-assign]
    return client


def _user(provider: str = "lmstudio", model: str = "test-model") -> User:
    """A signed-in user with a provider of their own.

    LM Studio because its definition ships a placeholder credential, so
    `AIConfig.for_user` resolves without a stored key and the test says nothing about
    encryption it is not about.
    """
    return User(
        id="u-1", email="tester@example.com", password_hash="x",
        ai_provider=provider, ai_model=model,
    )


# ------------------------------------------------------- the turn fits, by arithmetic

@pytest.mark.parametrize("window", WINDOWS)
def test_the_whole_turn_fits_the_window_it_was_sized_for(window):
    """Every contributor at its ceiling still lands inside the input budget.

    This is the property the division exists for. Before it, the per-tool ceilings were
    constants and the turn's total was whatever five of them happened to come to.
    """
    client = _client(window)

    total = client.resolve_turn_share() * ai._TURN_CONTRIBUTORS

    assert total <= client.resolve_context_budget()


@pytest.mark.parametrize("window", WINDOWS)
def test_a_search_response_never_exceeds_one_share(window):
    client = _client(window)

    assert sum(client._search_payload_budgets()) <= client.resolve_turn_share() * 4


@pytest.mark.parametrize("window", WINDOWS)
def test_the_two_retrieval_arms_together_spend_one_share(window):
    """`build_chat_context` reads `max_context_tokens // 4` once per arm, and runs two.

    So the field has to carry two halves of a share, not a share — otherwise the
    selected-circular context quietly costs double what the division allotted it.
    """
    client = _client(window)
    share = client.resolve_turn_share()

    per_arm = (share * 2) // 4

    assert share - 2 <= per_arm * 2 <= share


def test_a_small_window_gets_a_smaller_search_response():
    """The 8k local model that the fixed constants overflowed by 2.3x on one call."""
    small = _client(8_192)
    large = _client(1_310_720)

    assert sum(small._search_payload_budgets()) < sum(large._search_payload_budgets())
    # Five of them plus the selected-circular context, in tokens, inside the window.
    assert sum(small._search_payload_budgets()) // 4 * ai._TURN_CONTRIBUTORS <= 8_192


def test_a_window_with_room_keeps_the_tuned_search_ceilings():
    """Scaling is a reduction, never an inflation.

    Each constant records why that much of that corpus is worth reading; a million-token
    window is not a reason to hand back more, so above a share they pass through whole.
    """
    client = _client(1_310_720)

    assert client._search_payload_budgets() == (
        SEARCH_INLINE_BODY_BUDGET_CHARS,
        SEARCH_PASSAGE_BUDGET_CHARS,
        LAW_SEARCH_PASSAGE_BUDGET_CHARS,
    )


def test_the_arms_shrink_together_rather_than_one_absorbing_it():
    """40:24:8 is a judgement about the two corpora, and it survives the reduction."""
    inline, passage, law = _client(32_768)._search_payload_budgets()

    assert inline > passage > law
    assert inline / law == pytest.approx(
        SEARCH_INLINE_BODY_BUDGET_CHARS / LAW_SEARCH_PASSAGE_BUDGET_CHARS, rel=0.02
    )
    assert passage / law == pytest.approx(
        SEARCH_PASSAGE_BUDGET_CHARS / LAW_SEARCH_PASSAGE_BUDGET_CHARS, rel=0.02
    )


def test_no_arm_is_ever_budgeted_to_nothing():
    """A provider is free to report an implausible window; a zero budget is a crash."""
    for window in (1, 128, 512, 2_048):
        assert all(value > 0 for value in _client(window)._search_payload_budgets())


def test_the_scaled_budget_is_what_the_payload_builder_actually_spends():
    """The number has to reach the loop that spends it, not just be computed."""
    body = "x" * 900
    results = [
        {
            "result_kind": "circular",
            "circular": make_circular(f"c-{index}", content_text=body),
            "snippet": "...",
            "match_source": "circular",
        }
        for index in range(10)
    ]

    generous = AIClient._inline_body_texts(results, budget=10 * len(body))
    pinched = AIClient._inline_body_texts(results, budget=2 * len(body))

    assert len(generous) == 10
    assert len(pinched) == 2


# --------------------------------------------------- chat is sized by its own model

def test_chat_is_sized_by_the_model_not_the_dataclass_default(monkeypatch):
    """`get_ai_client_for_user` is the only place that can fill this in.

    `AIConfig.for_user` has no client to ask the provider with, so it left 4,000 —
    which meant a 1,310,720-token model grounded its answers on 1,000 tokens per arm.
    """
    monkeypatch.setattr(AIClient, "detect_context_window", lambda self: 131_072)

    client = get_ai_client_for_user(_user())

    assert client.config.max_context_tokens != AIConfig.max_context_tokens
    assert client.config.max_context_tokens == client.resolve_turn_share() * 2


def test_the_raw_window_is_not_what_lands_in_the_field(monkeypatch):
    """The other way this was wrong.

    `save_settings` stores the detected window as `ai_max_context_tokens`; copying that
    straight into the chat config is what produced the 927,622-character context,
    because every consumer of the field multiplies it back up.
    """
    monkeypatch.setattr(AIClient, "detect_context_window", lambda self: 1_310_720)

    client = get_ai_client_for_user(_user())

    assert client.config.max_context_tokens < 1_310_720
    assert client.config.max_context_tokens * 4 <= 1_310_720


def test_an_unreported_window_falls_back_rather_than_failing():
    """OpenAI and Google are never probed, and any provider can be briefly unreachable.

    The per-provider table is what the budget then rests on, so it has to be reached
    rather than divided into: an unreported window must not mean a zero share.
    """
    client = _client(None, provider="openai")

    fallback = ai._PROVIDER_CONTEXT_WINDOW["openai"]
    expected = int(fallback * ai._CONTEXT_INPUT_FRACTION) // ai._TURN_CONTRIBUTORS
    assert client.resolve_turn_share() == min(expected, ai._TURN_SHARE_MAX_TOKENS)


# ------------------------------------------------------------- probing the provider

class _FakeProvider:
    """An OpenAI-shaped stub that counts how often its catalogue is fetched."""

    def __init__(self, entries: list[dict] | None = None, fails: bool = False):
        self.entries = entries or [{"id": "test", "context_length": 32_768}]
        self.fails = fails
        self.probes = 0

    def with_options(self, **_kwargs):
        return self

    @property
    def models(self):
        return self

    def list(self):
        self.probes += 1
        if self.fails:
            raise RuntimeError("provider unreachable")
        return SimpleNamespace(data=list(self.entries))


def _probing_client(provider: _FakeProvider, model: str = "test") -> AIClient:
    """A client wired to `provider` without constructing a real SDK client."""
    client = AIClient.__new__(AIClient)
    client.config = AIConfig(provider="openrouter", api_key="test", model=model)
    client._client = provider
    client._structured_mode = "json_object"
    client._context_budget = None
    return client


def test_the_window_is_probed_once_not_once_per_turn():
    """A chat request builds its own client, so the per-instance memo never survives.

    Uncached, every turn — and every budget inside it — paid a `models.list()` round
    trip to the provider before it could ask how big the window was.
    """
    provider = _FakeProvider()

    for _ in range(5):
        assert _probing_client(provider).detect_context_window() == 32_768

    assert provider.probes == 1


def test_changing_the_model_probes_again():
    """The cache key names the models, because they are the only input to the answer."""
    provider = _FakeProvider([
        {"id": "small", "context_length": 32_768},
        {"id": "large", "context_length": 131_072},
    ])

    assert _probing_client(provider, model="small").detect_context_window() == 32_768
    assert _probing_client(provider, model="large").detect_context_window() == 131_072
    assert provider.probes == 2


def test_an_unreachable_provider_is_not_pinned_to_the_fallback(monkeypatch):
    """A failure is cached too, so a dead provider is not re-probed by every request —
    but briefly, so recovery does not have to wait out the success TTL."""
    provider = _FakeProvider(fails=True)
    now = [1_000.0]
    monkeypatch.setattr(ai.time, "monotonic", lambda: now[0])

    assert _probing_client(provider).detect_context_window() is None
    assert _probing_client(provider).detect_context_window() is None
    assert provider.probes == 1

    now[0] += ai._WINDOW_CACHE_FAILURE_TTL_SECONDS + 1
    provider.fails = False

    assert _probing_client(provider).detect_context_window() == 32_768
    assert provider.probes == 2
