# log_safety.py
# =============================================================================
# Helpers for keeping secrets out of logs and console output.
#
# Why this exists: until Release C, the startup banner logged DATABASE_URL in
# full, password included, so the production database password was readable
# by anyone with access to the Render logs. Every place that shows a database
# URL to a person must go through mask_db_url().
# =============================================================================
import re

_USERINFO_PASSWORD = re.compile(r"(://[^:/@\s]*:)[^@\s]*(@)")


def mask_db_url(url) -> str:
    """Return the URL with its password replaced by ***.

    Keeps the scheme, user, host, port and database name, so the log line is
    still useful for checking which database the app is talking to.
    Never raises: logging must not be able to crash startup.
    """
    if not url:
        return str(url)
    text = str(url)
    try:
        from sqlalchemy.engine import make_url

        parsed = make_url(text)
        if parsed.password is not None:
            text = parsed.render_as_string(hide_password=True)
    except Exception:
        pass
    # Belt and braces: also covers strings SQLAlchemy cannot parse.
    return _USERINFO_PASSWORD.sub(r"\1***\2", text)
