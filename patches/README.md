# blinkpy patches

The Docker build clones blinkpy from GitHub at the commit pinned in the
`Dockerfile` and applies everything in this directory, in filename order, with
`git apply`. Keep them numbered.

## 0001-harden-validate-homescreen.patch

`Blink.validate_homescreen()` upstream is one line:

```python
self.homescreen = await response.json()
```

That assumes the homescreen request succeeded and returned JSON. When Blink
answers with an HTML error or throttling page instead — which it does under
load — this raises `ContentTypeError` from deep inside the library, with no
status code and no body in the message, so the log says nothing about what
Blink actually replied. A `None` response raises `AttributeError` for the same
reason.

The patch checks the status, logs the first 200 characters of the body, and
raises `BlinkSetupError` in both cases.

This is diagnostics, not a functional fix: `monitor.py` catches broad
exceptions around `start()` and `refresh()`, so without the patch the app
degrades to the sign-in panel or a failed poll rather than crashing. It just
does so without telling you why.

Not upstream as of `dev` at c36646e (checked 2026-08-18), so it cannot be
dropped by moving the pin forward. Check before bumping.
