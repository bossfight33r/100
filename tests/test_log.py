from clipfactory.log import REDACTED, redact_processor, redact_text


def test_redacts_secret_keys_and_values():
    event = {
        "event": "x",
        "api_key": "abc",
        "token_ref": "yt_main",
        "headers": {"Authorization": "Bearer xyz"},
        "msg": "key sk-ant-api03-AAAA and 123456789:AAEhBP0av18Z_tokentokentokentokentoken",
    }
    out = redact_processor(None, "info", event)
    assert out["api_key"] == REDACTED
    assert out["token_ref"] == "yt_main"
    assert out["headers"]["Authorization"] == REDACTED
    assert "sk-ant" not in out["msg"] and "AAEhBP0" not in out["msg"]


def test_redact_text_google_token():
    assert "ya29" not in redact_text("token=ya29.a0AfH6SMB-xyz")


def test_redacts_telegram_token_inside_bot_api_url():
    url = (
        "https://api.telegram.org/file/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/videos/x.mp4"
    )
    out = redact_text(f"download failed: {url}")
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in out and "123456789:" not in out
