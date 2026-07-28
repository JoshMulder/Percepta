ACCESS_COOKIE_NAME = "percepta_access"

# HttpOnly so no script can read it, SameSite=Lax so it rides same-site
# navigations but not cross-site form posts. Secure is set from config rather
# than hardcoded so local HTTP development still works, but it must be on
# anywhere real - see set_access_cookie.
COOKIE_PATH = "/"


def set_access_cookie(response, token: str, *, max_age: int, secure: bool) -> None:
    response.set_cookie(
        key=ACCESS_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path=COOKIE_PATH,
    )


def clear_access_cookie(response) -> None:
    response.delete_cookie(key=ACCESS_COOKIE_NAME, path=COOKIE_PATH)
