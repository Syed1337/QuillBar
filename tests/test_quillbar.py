import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import quillbar as Q  # noqa: E402
config = hotkeys = llm = Q


@pytest.mark.parametrize("s,mods,vk", [
    ("Ctrl+Alt+R", hotkeys.MOD_CONTROL | hotkeys.MOD_ALT, ord("R")),
    ("ctrl+alt+space", hotkeys.MOD_CONTROL | hotkeys.MOD_ALT, 0x20),
    ("Win+Shift+F5", hotkeys.MOD_WIN | hotkeys.MOD_SHIFT, 0x74),
    ("F9", 0, 0x78),
    ("Alt+`", hotkeys.MOD_ALT, 0xC0),
    ("Ctrl++", hotkeys.MOD_CONTROL, 0xBB),
])
def test_parse(s, mods, vk):
    assert hotkeys.parse_hotkey(s) == (mods, vk)


@pytest.mark.parametrize("bad", ["", "R", "Hyper+R", "Ctrl+Ü"])
def test_parse_bad(bad):
    with pytest.raises(ValueError):
        hotkeys.parse_hotkey(bad)


def test_bare_allowed_for_temp():
    assert hotkeys.parse_hotkey("1", allow_bare=True) == (0, ord("1"))
    assert hotkeys.parse_hotkey("Esc", allow_bare=True) == (0, 0x1B)


def test_normalize():
    assert hotkeys.normalize("alt+ctrl+r") == "Ctrl+Alt+R"
    assert hotkeys.normalize("ctrl+alt+space") == "Ctrl+Alt+Space"


def test_clean_output():
    assert llm.clean_output("<think>hmm</think>\nHello there") == "Hello there"
    assert llm.clean_output("```\nHi\n```") == "Hi"
    assert llm.clean_output('"Hi Bob"', "hi bob") == "Hi Bob"
    assert llm.clean_output('"quoted"', '"quoted"') == '"quoted"'
    assert llm.clean_output("<text>\n你好\n</text>") == "你好"
    assert llm.clean_output("「こんにちは」", "こんにちは") == "こんにちは"


def test_split_ws():
    assert llm.split_ws("  hi there\n") == ("  ", "hi there", "\n")


def test_messages_shape():
    m = llm.build_messages("sys", "do x", "text")
    assert m[0]["role"] == "system" and "<text>\ntext\n</text>" in m[1]["content"]


def test_config_roundtrip(tmp_path):
    path = tmp_path / "c.json"
    c = config.Config.load(path)
    assert c.provider and c.actions and c.bindings()[config.TOOLBAR_KEY]
    c.provider["api_key"] = "sk-secret"
    c.save(path)
    raw = path.read_text()
    assert "sk-secret" not in raw and "api_key_enc" in raw
    c2 = config.Config.load(path)
    assert c2.provider["api_key"] == "sk-secret"
    assert json.loads(raw)["actions"][0]["name"] == "Rewrite"


def test_default_hotkeys_unique_and_valid():
    c = config.Config(dict(config.DEFAULTS, providers=[config.new_provider()],
                           active_provider=""))
    combos = [hotkeys.normalize(v) for v in c.bindings().values()]
    assert len(combos) == len(set(combos))
    for v in combos:
        hotkeys.parse_hotkey(v)


def test_complete_against_fake_server(monkeypatch):
    class R:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "Hello, I am testing this tool."}}]}
    seen = {}
    def fake_post(url, headers, json, timeout):
        seen.update(url=url, headers=headers, body=json)
        return R()
    monkeypatch.setattr(llm.requests, "post", fake_post)
    p = config.new_provider("DeepSeek")
    p["api_key"] = "k"
    out = llm.complete(p, "sys", "fix", "helo i am testting")
    assert out == "Hello, I am testing this tool."
    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer k"


def test_error_message(monkeypatch):
    class R:
        status_code = 401
        text = ""
        def json(self):
            return {"error": {"message": "Invalid key"}}
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: R())
    with pytest.raises(llm.LLMError, match="check API key"):
        llm.complete(config.new_provider(), "s", "i", "t")


@pytest.mark.parametrize("raw,want", [
    ("https://www.dmxapi.cn/v1/", "https://www.dmxapi.cn/v1"),
    ("https://www.dmxapi.cn/v1/chat/completions", "https://www.dmxapi.cn/v1"),
    (" www.dmxapi.cn/v1 ", "https://www.dmxapi.cn/v1"),
    ("http://localhost:11434/v1/models", "http://localhost:11434/v1"),
])
def test_normalize_base(raw, want):
    assert llm.normalize_base(raw) == want


def test_v1_fallback(monkeypatch):
    calls = []

    class R:
        def __init__(self, code):
            self.status_code = code
            self.headers = {"content-type": "application/json"}
            self.text = ""

        def json(self):
            return {"choices": [{"message": {"content": "Fixed."}}]}

    def fake_post(url, **k):
        calls.append(url)
        return R(200 if "/v1/" in url else 404)

    monkeypatch.setattr(llm.requests, "post", fake_post)
    p = config.new_provider("DMXAPI")
    p.update(base_url="https://www.dmxapi.cn", api_key="k")
    assert llm.complete(p, "s", "i", "t") == "Fixed."
    assert calls == ["https://www.dmxapi.cn/chat/completions",
                     "https://www.dmxapi.cn/v1/chat/completions"]
    assert p["base_url"] == "https://www.dmxapi.cn/v1"


def test_html_404_message(monkeypatch):
    class R:
        status_code = 404
        headers = {"content-type": "text/html"}
        text = "<html>"

        def json(self):
            raise ValueError

    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: R())
    p = config.new_provider()
    p.update(base_url="https://dmxapi.com/v1", api_key="k")
    with pytest.raises(llm.LLMError, match="web page"):
        llm.complete(p, "s", "i", "t")


def test_www_fallback(monkeypatch):
    calls = []

    class R:
        def __init__(self, code):
            self.status_code = code
            self.headers = {"content-type": "text/html" if code == 404 else "application/json"}
            self.text = ""

        def json(self):
            return {"choices": [{"message": {"content": "OK."}}]}

    def fake_post(url, **k):
        calls.append(url)
        return R(200 if url == "https://www.dmxapi.com/v1/chat/completions" else 404)

    monkeypatch.setattr(llm.requests, "post", fake_post)
    p = config.new_provider()
    p.update(base_url="https://dmxapi.com/", api_key="k")
    assert llm.complete(p, "s", "i", "t") == "OK."
    assert p["base_url"] == "https://www.dmxapi.com/v1"
    assert len(calls) == 4


def test_no_channel_hint(monkeypatch):
    class R:
        status_code = 503
        headers = {"content-type": "application/json"}
        text = ""

        def json(self):
            return {"error": {"message": "No available channel for model gemini-2.0-flash"}}

    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: R())
    with pytest.raises(llm.LLMError, match="Fetch models"):
        llm.complete(config.new_provider(), "s", "i", "t")


# ---------------------------------------------------------------- v2 features
diffview = prompts = Q
MouseWatcher = Q.MouseWatcher


def _cfg(**over):
    import copy as _c
    d = _c.deepcopy(config.DEFAULTS)
    d.update(providers=[config.new_provider()], **over)
    d["active_provider"] = d["providers"][0]["id"]
    return config.Config(d)


def test_placeholders_and_unknown_braces():
    c = _cfg(lang1="English", lang2="Japanese")
    out = prompts.instruction_for(c, "To {lang2} for {app}; keep {code} and {", "WeChat")
    assert out == "To {lang2} for {app}; keep {code} and {"  # stray brace -> untouched
    out = prompts.instruction_for(c, "To {lang2} for {app}; keep {code}", "WeChat")
    assert out == "To Japanese for WeChat; keep {code}"


def test_app_rule_and_style_in_system():
    c = _cfg(style="Sign as Alex.")
    sysp = prompts.system_for(c, {"kind": "transform"}, "WeChat")
    assert "Sign as Alex." in sysp and "chat message in WeChat" in sysp
    gen = prompts.system_for(c, {"kind": "generate"}, "OUTLOOK")
    assert "do NOT answer" not in gen and "email in OUTLOOK" in gen
    assert "chat message" not in prompts.system_for(c, None, "notepad")


def test_smart_select_apps():
    c = _cfg()
    assert prompts.is_smart_select_app(c, "WeChat")
    assert not prompts.is_smart_select_app(c, "WINWORD")


def test_redact_roundtrip():
    text = "Mail bob@x.com or call +86 138 0013 8000, card 4111 1111 1111 1111. Room 12."
    masked, m = prompts.redact(text)
    assert "bob@x.com" not in masked and "138" not in masked and "4111" not in masked
    assert "Room 12" in masked
    assert prompts.restore(masked, m) == text


def test_diff_and_counts():
    p = {"add_bg": "#0f0", "add_fg": "#000", "del_fg": "#f00"}
    h = diffview.diff_html("I has a cat", "I have a cat", p)
    assert "line-through" in h and "have" in h
    assert diffview.word_count("你好世界 hello") == 5
    assert diffview.change_ratio("same", "same") == 0


def test_mouse_gestures():
    now = 10.0
    assert MouseWatcher.classify((0, 0), (40, 2), 0.3, None, now)[0] == "drag"
    kind, rec = MouseWatcher.classify((5, 5), (5, 5), 0.05, None, now)
    assert kind == "click"
    assert MouseWatcher.classify((5, 5), (6, 5), 0.05, rec, now + 0.2)[0] == "double"
    assert MouseWatcher.classify((5, 5), (6, 5), 0.05, rec, now + 0.9)[0] == "click"
    assert MouseWatcher.classify((0, 0), (3, 3), 0.5, None, now)[0] == "click"  # tiny wiggle


def test_migration_adds_new_defaults_once(tmp_path):
    path = tmp_path / "c.json"
    old = {"actions": [{"id": "rewrite", "name": "Rewrite", "prompt": "x", "hotkey": "Ctrl+Alt+T"}]}
    path.write_text(json.dumps(old))
    c = config.Config.load(path)
    ids = [a["id"] for a in c["actions"]]
    assert "reply" in ids and "summarize" in ids
    tr = next(a for a in c["actions"] if a["id"] == "translate")
    assert tr["hotkey"] == ""  # Ctrl+Alt+T already taken by user's button
    c["actions"] = [a for a in c["actions"] if a["id"] != "reply"]  # user deletes Reply
    c.save(path)
    c2 = config.Config.load(path)
    assert "reply" not in [a["id"] for a in c2["actions"]]


def test_provider_override():
    c = _cfg()
    other = config.new_provider("DeepSeek")
    c["providers"].append(other)
    assert c.provider_for({"provider": other["id"]})["name"] == "DeepSeek"
    assert c.provider_for({"provider": "missing"}) is c.provider


def test_stream_sse(monkeypatch):
    lines = [b'data: {"choices":[{"delta":{"content":"Hel"}}]}', b"",
             b'data: {"choices":[{"delta":{"content":"lo."}}]}', b"data: [DONE]"]

    class R:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        def iter_lines(self, decode_unicode=False):
            return iter(lines)

    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: R())
    got = []
    out = llm.stream(_cfg().provider, "s", "i", "t", got.append)
    assert out == "Hello." and got == ["Hel", "lo."]


def test_stream_json_fallback(monkeypatch):
    class R:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"choices": [{"message": {"content": "Whole."}}]}

    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: R())
    assert llm.stream(_cfg().provider, "s", "i", "t", lambda d: None) == "Whole."
