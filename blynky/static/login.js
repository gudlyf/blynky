// The login page's only script: send the username and password, then go
// back to where the browser was headed. A file of its own, since the page's
// Content-Security-Policy allows no inline script.

// Only a path on this server, never //elsewhere or a scheme - the same rule
// the server applies to ?next=.
function nextPath() {
  const next = new URLSearchParams(location.search).get("next") || "/";
  let url;
  try {
    url = new URL(next, location.origin);
  } catch (err) {
    return "/";
  }
  if (url.origin !== location.origin) return "/";
  // A notification link is /#/event/...: the fragment never reaches the
  // server, but the browser carries it across the redirect to this page.
  return url.pathname + url.search + location.hash;
}

// Already signed in - another tab got there first, or the back button -
// means there is nothing to ask for: go straight on.
fetch("/api/auth")
  .then((response) => response.json())
  .then((auth) => {
    if (!auth.enabled || auth.signed_in) location.replace(nextPath());
  })
  .catch(() => {});

const form = document.getElementById("auth-form");
const button = document.getElementById("auth-submit");
const error = document.getElementById("auth-error");

function showError(message) {
  error.textContent = message || "";
  error.hidden = !message;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  button.disabled = true;
  button.textContent = "Signing in…";
  showError("");
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user: document.getElementById("user").value,
        password: document.getElementById("password").value,
      }),
    });
    const result = await response.json().catch(() => ({}));
    if (response.ok && result.status === "ok") {
      location.replace(nextPath());
      return;
    }
    showError(result.message || `Sign-in failed (${response.status}).`);
    document.getElementById("password").select();
  } catch (err) {
    showError("Could not reach Blynky.");
  }
  button.disabled = false;
  button.textContent = "Sign in";
});
