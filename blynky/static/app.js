// Blynky UI: a grid of camera stills that isolates one camera when
// motion is described.

const el = (id) => document.getElementById(id);

const state = {
  cameras: new Map(),   // name -> camera entry from the server
  selected: null,       // camera name currently isolated, or null for grid
  showingClip: false,
  showingLive: false,   // detail view is on the live still, not an event
  shownId: null,        // id of the event on screen, so its row can be marked
  snapping: new Set(),  // cameras with a new-picture request in flight
  events: [],           // recent motion across every camera, newest first
  feeds: new Map(),     // "camera|subject" -> that list of motion, newest first
  timeline: 25,         // how many rows either timeline shows
  peopleOnly: false,    // both timelines are showing only events with a person
  lastPoll: null,       // ISO time of the last completed motion check
  pollSeconds: 60,
  ntfy: null,           // whether the server's push notifications are paused
  pageSize: 60,
  status: "starting",   // starting | needs-credentials | awaiting-2fa | watching
};

// The backlog of saved clips with no description, and whether the server is
// working through it.
const backfill = { pending: 0, running: false };

// The manual pull of whatever clips Blink still holds. Blink only keeps a
// clip where the account's storage puts it, and a poll that was not running -
// or that the sync module was too busy to answer - never sees it at all.
const sync = { running: false, found: 0, saved: 0, at: null };

// The library view is a separate, paged read of everything on disk. It keeps
// its own list because the live feed above is only ever the recent tail.
const NO_FILTERS = { camera: "", kind: "", subject: "", q: "", until: "" };

const lib = {
  open: false,
  items: [],            // records loaded so far, newest first
  next: null,           // cursor for the following page, or null at the end
  loading: false,
  generation: 0,        // bumped per reload, so a superseded page is dropped
  filters: { ...NO_FILTERS },
  viewing: null,        // index into items, or null when the viewer is closed
  closing: false,       // the viewer is on its way out through history.back()
  stale: false,         // the archive moved on while this view was closed
};

// --------------------------------------------------------------- preferences

// Choices that belong to this browser rather than to the server: a wall
// display wants different things from a phone. Storage can be missing or
// refuse (private windows), and nothing here is worth failing over.
const PREFS_KEY = "blynky.";
const prefs = {
  follow: loadPref("follow", true),
  peopleOnly: loadPref("peopleOnly", false),
  theme: loadPref("theme", "auto"),
  notify: loadPref("notify", false),
  wake: loadPref("wake", false),
  sound: loadPref("sound", false),
  ntfyHours: loadPref("ntfyHours", "8"),
};

function loadPref(name, fallback) {
  try {
    const raw = localStorage.getItem(PREFS_KEY + name);
    if (raw === null) return fallback;
    if (typeof fallback === "boolean") return raw === "true";
    return raw;
  } catch (err) {
    return fallback;
  }
}

function savePref(name, value) {
  prefs[name] = value;
  try {
    localStorage.setItem(PREFS_KEY + name, String(value));
  } catch (err) {
    // Remembered for this page only, then.
  }
}

// ---------------------------------------------------------------- rendering

function icon(name, className = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function pill(text, extra) {
  const span = document.createElement("span");
  span.className = extra ? `pill ${extra}` : "pill";
  span.textContent = text;
  return span;
}

function cameraCard(camera) {
  const card = document.createElement("article");
  card.className = "card";
  card.dataset.name = camera.name;

  const thumb = document.createElement("div");
  thumb.className = "thumb";
  if (camera.image) {
    const img = document.createElement("img");
    img.src = camera.image;
    img.alt = "";
    thumb.append(img);
  } else {
    const ph = document.createElement("div");
    ph.className = "placeholder";
    ph.textContent = "no image yet";
    thumb.append(ph);
  }

  // A disarmed sync module reports no motion at all, and an offline camera
  // cannot take a picture; both would otherwise just look quiet.
  const flags = document.createElement("div");
  flags.className = "flags";
  if (!camera.armed) flags.append(pill("Disarmed", "warn"));
  if (camera.online === false) flags.append(pill("Offline", "bad"));
  if (flags.childElementCount) thumb.append(flags);

  // Blink only stores a new thumbnail when something asks for one, so a still
  // can be months old. Say how old it is rather than implying it is live.
  if (camera.captured) {
    const age = pill(relAge(camera.captured), "age");
    if (ageSeconds(camera.captured) > 3600) age.classList.add("stale");
    age.title = `Taken ${new Date(camera.captured).toLocaleString()}`;
    thumb.append(age);
  }

  thumb.append(snapButton(camera));

  const foot = document.createElement("div");
  foot.className = "card-foot";

  // The name is the card's one real button, stretched over the whole card,
  // so the card can be reached and opened from the keyboard.
  const name = document.createElement("button");
  name.type = "button";
  name.className = "name card-open";
  name.textContent = camera.name;
  if (camera.sync) name.title = `${camera.name} - ${camera.sync}`;

  const meta = document.createElement("span");
  meta.className = "meta";
  if (camera.last_event) {
    meta.textContent = friendlyTime(camera.last_event.stamp);
    meta.title = `Last motion ${fullTime(camera.last_event.stamp)}`;
  }

  foot.append(name, meta);

  // What the last motion was, so the grid alone answers "anything worth
  // opening?" without opening every camera.
  const last = camera.last_event;
  if (last) {
    const line = document.createElement("span");
    line.className = "last";
    if (last.who && last.who.length) {
      const who = document.createElement("span");
      who.className = last.who_confidence === "confirmed" ? "tl-who sure" : "tl-who";
      who.textContent = last.who.join(" & ");
      line.append(who);
    }
    if (last.summary) line.append(...describe(last.summary));
    else line.append(last.state === "describing" ? "Describing…" : "No description");
    line.title = last.summary || "";
    foot.append(line);
  }

  card.append(thumb, foot);
  card.addEventListener("click", () => transition(() => isolate(camera.name)));
  return card;
}

function snapButton(camera) {
  const button = document.createElement("button");
  button.type = "button";
  const busy = state.snapping.has(camera.name);
  button.className = busy ? "snap working" : "snap";
  button.disabled = busy || !camera.online;
  button.append(icon("shutter"), busy ? "Taking…" : "Refresh");
  button.title = camera.online
    ? "Wake this camera and take a new picture"
    : "Camera is offline";
  button.setAttribute("aria-label", `${button.title}: ${camera.name}`);
  // The card itself opens the detail view; the button must not.
  button.addEventListener("click", (e) => {
    e.stopPropagation();
    snap(camera.name);
  });
  return button;
}

// Waking a camera costs battery, so a new still is only ever taken when
// someone asks for one.
async function snap(name) {
  if (state.snapping.has(name)) return;
  state.snapping.add(name);
  refreshCamera(name);

  try {
    const result = await post("/api/snap", { camera: name });
    state.snapping.delete(name);
    if (result.status === "ok" && result.camera) {
      state.cameras.set(name, result.camera);
      if (state.selected === name) isolate(name, null, true);
    } else if (result.status !== "ok") {
      showMessage(`Could not refresh ${name}`, result.message || "Blink said no.");
    }
  } catch (err) {
    state.snapping.delete(name);
    showMessage(`Could not refresh ${name}`, "The server is unreachable.");
  }
  refreshCamera(name);
}

function refreshCamera(name) {
  const camera = state.cameras.get(name);
  if (camera) updateCard(camera);
  if (state.selected === name) renderDetailTools();
}

function renderGrid() {
  const grid = el("grid");
  // Until the server has said it is watching, an empty list means "not
  // heard yet", so the placeholders stay.
  if (!state.cameras.size && state.status !== "watching") return;
  grid.setAttribute("aria-busy", "false");
  if (!state.cameras.size) {
    const empty = document.createElement("p");
    empty.className = "grid-empty";
    empty.textContent = "Blink has not reported any cameras yet.";
    grid.replaceChildren(empty);
    return;
  }
  grid.replaceChildren(...[...state.cameras.values()].map(cameraCard));
}

function updateCard(camera) {
  const card = document.querySelector(`.card[data-name="${cssEscape(camera.name)}"]`);
  if (!card) {
    renderGrid();
    return;
  }
  const focused = card.contains(document.activeElement);
  const fresh = cameraCard(camera);
  card.replaceWith(fresh);
  // Redrawing a card must not throw keyboard focus back to the top.
  if (focused) fresh.querySelector(".card-open").focus({ preventScroll: true });
}

// ------------------------------------------------------------- detail panel

function isolate(name, event, live) {
  const camera = state.cameras.get(name);
  if (!camera) return;
  // Going from one camera to another replaces the history entry; arriving
  // from the grid or the library adds one, so Back returns there.
  const fromDetail = state.selected !== null && !lib.open;
  // The library covers the same screen area, so leave it before taking over.
  if (lib.open) closeLibrary({ quiet: true });

  state.selected = name;
  state.showingClip = false;
  // A caller that knows which picture it wants says so. A plain click on a
  // card asks for whatever that card is showing, and "source" is the server's
  // record of which picture that is: a still someone asked for, or a frame cut
  // out of the last clip. Defaulting to the event instead would answer a
  // refresh by opening a clip from hours ago.
  state.showingLive =
    live === undefined && !event ? camera.source === "snapshot" : Boolean(live);

  // Prefer the event that was just described; fall back to the camera's
  // most recent one, then to the live still.
  const shown = state.showingLive ? null : event || camera.last_event;

  el("detail-name").textContent = name;
  // Which Blink system it is in: arming is per system, so it matters.
  el("detail-system").textContent = camera.sync || "";
  const stamp = el("detail-stamp");
  stamp.textContent = shown
    ? friendlyTime(shown.stamp)
    : camera.captured
      ? `still, ${relAge(camera.captured)}`
      : "still";
  stamp.title = shown ? fullTime(shown.stamp) : "";

  const img = el("detail-image");
  const video = el("detail-video");
  const toggle = el("toggle-media");

  img.src = (shown && shown.image) || camera.image || "";
  img.alt = shown ? `${name} at ${fullTime(shown.stamp)}` : `${name}, latest still`;
  img.hidden = false;
  video.hidden = true;
  video.pause();
  video.removeAttribute("src");

  if (shown && shown.clip) {
    toggle.hidden = false;
    toggle.textContent = "Play clip";
    toggle.onclick = () => toggleMedia(shown.clip);
  } else if (state.showingLive && camera.last_event) {
    // Looking at the newer still, but the last event is still worth one
    // click. Switching to it brings its own description along, which playing
    // the clip in place under this still's caption would not.
    toggle.hidden = false;
    toggle.textContent = "Last motion";
    toggle.onclick = () => isolate(name, camera.last_event);
  } else {
    toggle.hidden = true;
    toggle.onclick = null;
  }
  el("stage-play").hidden = !(shown && shown.clip);

  const download = el("detail-download");
  const media = (shown && (shown.clip || shown.image)) || camera.image;
  download.hidden = !media;
  if (media) download.href = media;
  else download.removeAttribute("href");

  const names = [...state.cameras.keys()];
  el("detail-count").textContent =
    names.length > 1 ? `${names.indexOf(name) + 1} / ${names.length}` : "";
  el("detail-prev").disabled = names.length < 2;
  el("detail-next").disabled = names.length < 2;

  renderDetailTools();
  renderSummary(shown, camera);

  // Which row to mark as the one on screen, and the rows themselves.
  state.shownId = shown ? shown.id : null;
  renderDetailTimeline(name);
  loadCameraTimeline(name);

  el("home").hidden = true;
  el("detail").hidden = false;
  renderTabs();
  // Looking at the camera answers its popup.
  dismissToast(`motion:${name}`);
  syncHistory(fromDetail ? "replace" : "push");
  if (!fromDetail) window.scrollTo(0, 0);
}

// The cameras are in the order the server lists them, which is the grid's
// order too, and stepping off either end wraps around.
function stepCamera(delta) {
  const names = [...state.cameras.keys()];
  if (!state.selected || names.length < 2) return;
  const at = names.indexOf(state.selected);
  isolate(names[(at + delta + names.length) % names.length]);
}

function renderDetailTools() {
  const camera = state.cameras.get(state.selected);
  const button = el("detail-snap");
  if (!camera) {
    button.hidden = true;
    return;
  }
  const busy = state.snapping.has(camera.name);
  button.hidden = false;
  button.disabled = busy || !camera.online;
  button.textContent = busy ? "Taking…" : "New picture";
  button.onclick = () => snap(camera.name);
}

function toggleMedia(clipUrl) {
  const img = el("detail-image");
  const video = el("detail-video");
  const toggle = el("toggle-media");

  state.showingClip = !state.showingClip;
  el("stage-play").hidden = state.showingClip;
  if (state.showingClip) {
    video.src = clipUrl;
    video.hidden = false;
    img.hidden = true;
    toggle.textContent = "Show still";
    video.play().catch(() => {
      // Autoplay can be blocked; the controls are there either way.
    });
  } else {
    video.pause();
    video.hidden = true;
    img.hidden = false;
    toggle.textContent = "Play clip";
  }
}

function renderSummary(event, camera) {
  const box = el("summary");
  box.classList.remove("pending", "empty");

  if (!event) {
    box.classList.add("empty");
    // A still has nothing to describe, but saying there is no motion event
    // would be a lie whenever the camera has one waiting behind the button.
    box.textContent =
      camera && camera.last_event
        ? `Latest still. The last motion was ${friendlyTime(camera.last_event.stamp).toLowerCase()}.`
        : "No motion event recorded for this camera yet.";
    return;
  }
  if (event.state === "describing") {
    box.classList.add("pending");
    box.textContent = "Describing…";
    return;
  }
  if (!event.summary) {
    box.classList.add("empty");
    box.textContent = "No description available for this event.";
    return;
  }
  box.replaceChildren(...describe(event.summary));
}

function stopDetailVideo() {
  const video = el("detail-video");
  video.pause();
  video.removeAttribute("src");
}

function backToGrid() {
  const was = state.selected;
  state.selected = null;
  stopDetailVideo();
  el("detail").hidden = true;
  el("home").hidden = false;
  renderTabs();
  syncHistory("push");
  // Land back on the card that was open, for anyone moving by keyboard.
  if (was) {
    const card = document.querySelector(`.card[data-name="${cssEscape(was)}"] .card-open`);
    if (card) card.focus({ preventScroll: true });
  }
}

function renderTabs() {
  const [on, off] = lib.open
    ? [el("open-library"), el("tab-cameras")]
    : [el("tab-cameras"), el("open-library")];
  on.setAttribute("aria-current", "page");
  off.removeAttribute("aria-current");
}

// ----------------------------------------------------------------- timeline

// Two lists, one renderer: what has moved lately across every camera, under
// the grid, and one camera's own history, under its picture. Both are motion
// only - a still nobody has looked at is library material, not activity.

// Words that mean a human being. Highlighting them lets a list of events be
// scanned for the ones worth opening: most of what these cameras notice is
// animals, vehicles and weather, and those read the same as anything else.
// The optional first group is why "no people, animals or vehicles visible" -
// the model's way of saying the clip was empty - is matched but left plain:
// highlighting it would say the exact opposite. It is captured rather than
// looked behind for, which every browser can parse.
const PEOPLE =
  /(\b(?:no|without)\s+)?\b(?:persons?|people|man|men|woman|women|child(?:ren)?|kids?|boys?|girls?|someone|somebody|individuals?|adults?)\b/gi;

// textContent, except the words above come out wrapped. Built out of nodes
// rather than innerHTML: a description is model output, and it is text.
function describe(text) {
  const parts = [];
  let at = 0;
  for (const match of text.matchAll(PEOPLE)) {
    if (match.index > at) parts.push(document.createTextNode(text.slice(at, match.index)));
    if (match[1]) {
      parts.push(document.createTextNode(match[0]));
    } else {
      const who = document.createElement("mark");
      who.textContent = match[0];
      parts.push(who);
    }
    at = match.index + match[0].length;
  }
  if (at < text.length) parts.push(document.createTextNode(text.slice(at)));
  return parts;
}

// Rows and tiles are list items rather than buttons, so they are given the
// keyboard behaviour of one.
function activatable(node, open) {
  node.tabIndex = 0;
  node.addEventListener("click", open);
  node.addEventListener("keydown", (e) => {
    if (e.target !== node) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      open();
    }
  });
}

// `burst`, when given, makes this the one row standing for several clips:
// { newest, count, key, open, current }.
function timelineRow(record, withCamera, open, burst) {
  const row = document.createElement("li");
  const current = burst ? burst.current : record.id === state.shownId;
  row.className = current ? "tl-row current" : "tl-row";
  row.dataset.id = record.id;

  const picture = record.thumb || record.image;
  if (picture) {
    const thumb = document.createElement("img");
    thumb.className = "tl-thumb";
    thumb.src = picture;
    thumb.alt = "";
    thumb.loading = "lazy";
    row.append(thumb);
  } else {
    const thumb = document.createElement("span");
    thumb.className = "tl-thumb";
    thumb.append(icon(record.clip ? "play" : "shutter"));
    row.append(thumb);
  }

  const meta = document.createElement("span");
  meta.className = "tl-meta";
  // A burst is dated by its newest clip, which is where it sits in the list.
  const stamp = burst ? burst.newest.stamp : record.stamp;
  const time = document.createElement("time");
  time.className = "tl-time";
  time.dateTime = stamp;
  time.textContent = friendlyTime(stamp);
  time.title = fullTime(stamp);
  // The time and, for a burst, its clip count share the time's column.
  const when = document.createElement("span");
  when.className = "tl-when";
  when.append(time);
  meta.append(when);

  if (withCamera) {
    const name = document.createElement("span");
    name.className = "tl-camera";
    name.textContent = record.camera;
    meta.append(name);
  }
  row.append(meta);

  const text = document.createElement("span");
  const describing = record.state === "describing";
  text.className = record.summary ? "tl-text" : `tl-text ${describing ? "pending" : "empty"}`;
  // A recognised name leads the row: it is the thing being scanned for, and
  // it stays hedged so nobody reads a guess as a fact.
  if (record.who && record.who.length) {
    const name = document.createElement("span");
    name.className = "tl-who";
    name.textContent = record.who.join(" & ");
    name.title =
      record.who_confidence === "confirmed"
        ? "Confirmed by hand"
        : record.who_confidence
          ? `Probably - ${record.who_confidence} confidence`
          : "A guess, not a certainty";
    if (record.who_confidence === "confirmed") name.className = "tl-who sure";
    text.append(name, document.createTextNode(" "));
  }
  if (record.summary) text.append(...describe(record.summary));
  else text.append(describing ? "Describing…" : "No description");
  row.append(text);

  if (burst) {
    row.dataset.burst = burst.key;
    const more = document.createElement("button");
    more.type = "button";
    more.className = "tl-burst";
    more.setAttribute("aria-expanded", String(burst.open));
    more.title = burst.open ? "Fold these clips back into one row" : "Show every clip";
    more.append(`${burst.count} clips`, icon("next", "icon chevron"));
    more.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleBurst(burst.key);
    });
    when.append(more);
  }

  activatable(row, open);
  return row;
}

// ------------------------------------------------------------------ bursts

// Someone walking past sets a camera off several times, so one visit is a
// run of clips. Measured on this archive, clips from one camera never start
// within 15 seconds of each other and the gaps pile up through the first two
// minutes before settling into a background rate, so a clip that follows the
// previous one within that is taken as the same visit. The chain can run on:
// a camera facing a swaying branch for an hour is one row, which is the point.
const BURST_SECONDS = 120;

// Bursts someone has opened out, by their oldest clip's id - the one member
// that never changes as a burst grows.
const openBursts = new Set();

// Newest-first records into newest-first groups, one camera per group. Other
// cameras' clips may fall in between; a burst is still one row, placed at its
// newest clip.
function groupBursts(records) {
  const groups = [];
  const latest = new Map(); // camera -> the group its next-older clip may join
  for (const record of records) {
    const at = new Date(record.stamp).getTime();
    const group = latest.get(record.camera);
    if (group && group.oldestAt - at <= BURST_SECONDS * 1000) {
      group.members.push(record);
      group.oldestAt = at;
    } else {
      const fresh = { members: [record], oldestAt: at };
      groups.push(fresh);
      latest.set(record.camera, fresh);
    }
  }
  return groups;
}

// The clip that stands for a burst: the first one with a person in it, as
// that is what anyone scanning the list is looking for; failing that, the
// first described one, which shows what arrived rather than what was left.
function burstLead(members) {
  const oldestFirst = [...members].reverse();
  return (
    oldestFirst.find((m) => (m.subjects || []).includes("person")) ||
    oldestFirst.find((m) => m.summary) ||
    members[0]
  );
}

function burstRows(records, withCamera, openFor) {
  const rows = [];
  for (const { members } of groupBursts(records)) {
    if (members.length === 1) {
      rows.push(timelineRow(members[0], withCamera, openFor(members[0])));
      continue;
    }
    const key = members[members.length - 1].id;
    const lead = burstLead(members);
    const open = openBursts.has(key);
    rows.push(
      timelineRow(lead, withCamera, openFor(lead), {
        newest: members[0],
        count: members.length,
        key,
        open,
        // Folded, the row stands for all of them; open, each marks itself.
        current: !open && members.some((m) => m.id === state.shownId),
      })
    );
    if (open) {
      for (const member of members) {
        const row = timelineRow(member, withCamera, openFor(member));
        row.classList.add("member");
        rows.push(row);
      }
    }
  }
  return rows;
}

function toggleBurst(key) {
  if (openBursts.has(key)) openBursts.delete(key);
  else openBursts.add(key);
  renderTimeline();
  if (state.selected) renderDetailTimeline(state.selected);
  // Redrawing replaced the button that was pressed; hand focus to its twin.
  const button = document.querySelector(
    `#${state.selected ? "detail-timeline" : "timeline"} [data-burst="${cssEscape(key)}"] .tl-burst`
  );
  if (button) button.focus({ preventScroll: true });
}

// The lists are keyed by camera and by whether people are being filtered for,
// so switching the toggle back and forth does not refetch what is already
// held. An empty camera means the shared list under the grid.
function feedKey(camera) {
  return `${camera || ""}|${state.peopleOnly ? "person" : ""}`;
}

// A record with no subjects at all has not been described yet. Hiding those
// would make a live event vanish for a minute and then reappear, so the
// filter only removes what is known to have no person in it.
function hasPerson(record) {
  return !record.subjects || record.subjects.includes("person");
}

// Unfiltered, the list under the grid is the feed the page was handed on
// load - already in memory, and free. Filtered, it has to come from the
// archive: 25 recent events might hold only three with a person in them.
function timelineRows() {
  if (!state.peopleOnly) return state.events;
  return state.feeds.get(feedKey("")) || null;
}

function renderTimeline() {
  const held = timelineRows();
  const rows = (held || []).slice(0, state.timeline);
  el("timeline-list").replaceChildren(
    ...burstRows(rows, true, (record) => () =>
      transition(() => isolate(record.camera, record))
    )
  );
  el("timeline-empty").hidden = rows.length > 0 || !Array.isArray(held);
  el("timeline-empty").textContent = state.peopleOnly
    ? "No recent motion with a person in it."
    : "Nothing has moved yet.";
  el("timeline-note").textContent = rows.length ? `last ${rows.length}` : "";
}

// Both checkboxes drive one state, so the two lists can never disagree about
// what they are showing.
async function setPeopleOnly(on) {
  state.peopleOnly = Boolean(on);
  savePref("peopleOnly", state.peopleOnly);
  el("timeline-people").checked = state.peopleOnly;
  el("detail-timeline-people").checked = state.peopleOnly;
  renderTimeline();
  if (state.selected) renderDetailTimeline(state.selected);
  await Promise.all([
    state.peopleOnly ? loadTimelineFeed("") : Promise.resolve(),
    state.selected ? loadTimelineFeed(state.selected) : Promise.resolve(),
  ]);
}

function renderDetailTimeline(name) {
  const held = state.feeds.get(feedKey(name));
  const rows = (held || []).slice(0, state.timeline);
  el("detail-timeline-list").replaceChildren(
    ...burstRows(rows, false, (record) => () => isolate(name, record))
  );
  // Null means the fetch is still out. Saying "no motion recorded" before the
  // answer arrives would be a guess, and usually the wrong one.
  el("detail-timeline-empty").hidden = rows.length > 0 || !Array.isArray(held);
  el("detail-timeline-empty").textContent = state.peopleOnly
    ? "No motion with a person in it for this camera."
    : "No motion recorded for this camera yet.";
  el("detail-timeline-note").textContent = rows.length ? `last ${rows.length}` : "";
}

async function loadCameraTimeline(name) {
  if (state.cameras.has(name)) await loadTimelineFeed(name);
}

// One fetch for either list: the shared one when camera is empty, otherwise
// that camera's own. A camera's timeline comes off the archive rather than
// the shared feed because that feed holds the last few events across every
// camera at once, which for a quiet camera can be none of them.
async function loadTimelineFeed(camera) {
  const key = feedKey(camera);
  if (state.feeds.has(key)) return;
  // Claim the slot before awaiting. isolate() runs again on every socket
  // message, and each run would otherwise start a request of its own.
  state.feeds.set(key, null);

  const slug = camera ? (state.cameras.get(camera) || {}).slug : null;
  if (camera && !slug) {
    state.feeds.delete(key);
    return;
  }
  const params = new URLSearchParams({ kind: "motion", limit: state.timeline });
  if (slug) params.set("camera", slug);
  if (state.peopleOnly) params.set("subject", "person");

  try {
    const page = await getJSON(`/api/library?${params}`);
    state.feeds.set(key, page.events || []);
  } catch (err) {
    // Leave nothing behind, so asking again tries afresh.
    state.feeds.delete(key);
    return;
  }
  if (!camera) renderTimeline();
  else if (state.selected === camera) renderDetailTimeline(camera);
}

// A description landing later belongs on the entry already there; a new event
// goes on top. Either way the list stays capped at what the timeline shows.
function recordEvent(list, event) {
  const at = list.findIndex((item) => item.id === event.id);
  if (at >= 0) {
    list[at] = { ...list[at], ...event };
    return list;
  }
  // Insert where the stamp belongs rather than at the front. Live motion is
  // newest and lands at the top either way, but a description finishing for a
  // clip captured weeks ago must not push it to the head of "recent motion".
  const when = new Date(event.stamp);
  const before = list.findIndex((item) => new Date(item.stamp) < when);
  if (before >= 0) list.splice(before, 0, event);
  else if (list.length < state.timeline) list.push(event);
  if (list.length > state.timeline) list.length = state.timeline;
  return list;
}

// Fold one event into every list that should be holding it: the in-memory
// feed, and whichever fetched lists it belongs to. An event with no person
// belongs in no people-only list, which is the one case a plain merge would
// get wrong.
function mergeIntoFeeds(event) {
  recordEvent(state.events, event);
  for (const [key, held] of state.feeds) {
    if (!Array.isArray(held)) continue;
    const [camera, subject] = key.split("|");
    if (camera && camera !== event.camera) continue;
    if (subject && !hasPerson(event)) continue;
    recordEvent(held, event);
  }
}

// A description that landed on a clip captured long ago. It belongs on
// whatever row is already showing that event, and in any list it now
// qualifies for, but nothing about it is news: no card flashes, no camera is
// opened, and recordEvent files it by its own stamp, not at the top.
function updateEventInPlace(event) {
  mergeIntoFeeds(event);
  renderTimeline();
  if (state.selected) renderDetailTimeline(state.selected);
  if (state.shownId === event.id) {
    renderSummary(event, state.cameras.get(event.camera));
  }
}

// ------------------------------------------------------------------ library

async function openLibrary() {
  lib.open = true;
  // The camera that was open is left behind, and so is anything it was
  // playing: a clip carrying on unseen behind the archive is just noise.
  state.selected = null;
  stopDetailVideo();
  el("home").hidden = true;
  el("detail").hidden = true;
  el("library").hidden = false;
  renderTabs();
  syncHistory("push");
  loadStats();
  refreshBackfill();
  loadPeople();
  // Coming back to a view that has fallen behind has to refetch; coming back
  // to one that is still current keeps its pages, and its scroll position.
  if (!lib.items.length || lib.stale) await reloadLibrary();
}

// Open the library already narrowed down, e.g. to the camera on screen.
function openLibraryWith(filters) {
  const next = { ...NO_FILTERS, ...filters };
  if (!sameFilters(next, lib.filters)) {
    lib.filters = next;
    lib.stale = true;
  }
  setFilterControls();
  return openLibrary();
}

function sameFilters(a, b) {
  return Object.keys(NO_FILTERS).every((key) => (a[key] || "") === (b[key] || ""));
}

// How many saved clips still have no description, and a way to start on them.
// Hidden when there is nothing to do, which is the normal state.
async function refreshBackfill() {
  try {
    const status = await getJSON("/api/backfill");
    backfill.pending = status.pending;
    backfill.running = status.running;
  } catch (err) {
    el("describe-missing").hidden = true;
    return;
  }
  renderBackfill();
}

function renderBackfill() {
  const button = el("describe-missing");
  button.hidden = !backfill.pending;
  button.disabled = backfill.running;
  button.textContent = backfill.running
    ? `Describing ${backfill.pending}…`
    : `Describe ${backfill.pending} missing`;
}

// --------------------------------------------------------------- sync now

function renderSync() {
  const button = el("sync-now");
  const label = el("sync-label");
  button.disabled = sync.running;
  button.classList.toggle("running", sync.running);
  button.setAttribute("aria-busy", String(sync.running));
  if (sync.running) {
    label.textContent = sync.found
      ? `Syncing ${sync.saved}/${sync.found}…`
      : "Syncing…";
    return;
  }
  // Say what the last run found, since the usual answer is "nothing new" and
  // a button that silently returns to its label looks like it did nothing.
  label.textContent = sync.at
    ? sync.saved
      ? `Synced ${sync.saved} new`
      : "Nothing new"
    : "Sync now";
  button.setAttribute(
    "aria-label",
    sync.at ? `Sync now (last run: ${label.textContent.toLowerCase()})` : "Sync now"
  );
}

// A run started before this page loaded is still running on the server, so
// the button has to be told what it is looking at rather than assume idle.
async function refreshSync() {
  try {
    Object.assign(sync, await getJSON("/api/sync"));
  } catch (err) {
    return;
  }
  renderSync();
}

async function startSync() {
  if (sync.running) return;
  sync.running = true;
  sync.found = 0;
  sync.saved = 0;
  renderSync();
  try {
    const result = await post("/api/sync", {});
    if (result.status === "error") {
      sync.running = false;
      showMessage("Could not sync", result.message || "The server said no.");
    } else {
      Object.assign(sync, result);
    }
  } catch (err) {
    sync.running = false;
    showMessage("Could not sync", "The server is unreachable.");
  }
  renderSync();
}

async function startBackfill() {
  backfill.running = true;
  renderBackfill();
  try {
    const status = await post("/api/backfill", {});
    backfill.pending = status.pending;
    backfill.running = status.running;
  } catch (err) {
    backfill.running = false;
  }
  renderBackfill();
}

function closeLibrary(options = {}) {
  lib.open = false;
  closeViewerNow();
  el("library").hidden = true;
  el("home").hidden = false;
  state.selected = null;
  renderTabs();
  if (!options.quiet) syncHistory("push");
}

async function loadStats() {
  try {
    const stats = await getJSON("/api/library/stats");

    // Rebuild the camera filter from what the archive actually holds, not
    // from the cameras Blink reports now: a camera that has been removed
    // still has months of footage worth looking at.
    const select = el("filter-camera");
    const chosen = lib.filters.camera;
    select.replaceChildren();
    const all = document.createElement("option");
    all.value = "";
    all.textContent = "All cameras";
    select.append(all);
    for (const camera of stats.cameras) {
      const option = document.createElement("option");
      option.value = camera.slug;
      option.textContent = `${camera.camera} (${camera.count})`;
      select.append(option);
    }
    select.value = chosen;

    // The date picker cannot usefully offer days outside the archive.
    const until = el("filter-until");
    until.min = stats.oldest ? localDate(stats.oldest) : "";
    until.max = stats.newest ? localDate(stats.newest) : "";

    const parts = [`${stats.total} saved`];
    if (stats.stills) parts.push(`${stats.motion} motion, ${stats.stills} stills`);
    parts.push(humanBytes(stats.bytes));
    if (stats.oldest) parts.push(`since ${shortTime(stats.oldest)}`);
    el("library-stats").textContent = parts.join(" · ");
  } catch (err) {
    el("library-stats").textContent = "";
  }
}

async function reloadLibrary() {
  // A page still on its way was asked for under the old filters. It is let
  // finish and thrown away, rather than blocking this one - which is what
  // happened when two filters changed in quick succession.
  lib.generation += 1;
  lib.loading = false;
  lib.items = [];
  lib.next = null;
  lib.stale = false;
  el("library-grid").replaceChildren();
  el("library-empty").hidden = true;
  await loadLibraryPage();
}

async function loadLibraryPage() {
  if (lib.loading) return;
  lib.loading = true;
  const generation = lib.generation;
  const more = el("library-more");
  more.disabled = true;
  more.textContent = "Loading…";
  el("library-grid").setAttribute("aria-busy", "true");

  const params = new URLSearchParams();
  if (lib.filters.camera) params.set("camera", lib.filters.camera);
  if (lib.filters.kind) params.set("kind", lib.filters.kind);
  if (lib.filters.subject) params.set("subject", lib.filters.subject);
  if (lib.filters.q) params.set("q", lib.filters.q);
  if (lib.filters.until) params.set("until", endOfDay(lib.filters.until));
  if (lib.next) params.set("cursor", lib.next);
  params.set("limit", String(state.pageSize));

  let page = null;
  try {
    page = await getJSON(`/api/library?${params}`);
  } catch (err) {
    if (generation === lib.generation) {
      showMessage("Could not load the library", "The server is unreachable.");
    }
  }
  if (generation !== lib.generation) return;
  try {
    if (page) {
      appendLibrary(page.events || []);
      lib.next = page.next || null;
    }
  } finally {
    lib.loading = false;
    more.disabled = false;
    more.textContent = "Load more";
    el("library-grid").setAttribute("aria-busy", "false");
    renderLibraryFooter();
  }
}

function appendLibrary(records) {
  const grid = el("library-grid");
  // Day headings only make sense against what is already on screen, so the
  // last item of the previous page seeds the comparison.
  let previous = lib.items.length ? lib.items[lib.items.length - 1] : null;

  for (const record of records) {
    if (!previous || dayKey(record.stamp) !== dayKey(previous.stamp)) {
      const heading = document.createElement("h3");
      heading.className = "day";
      heading.textContent = dayLabel(record.stamp);
      grid.append(heading);
    }
    lib.items.push(record);
    grid.append(libraryTile(record));
    previous = record;
  }
  el("library-empty").hidden = lib.items.length > 0;
  el("library-empty").textContent = hasFilters()
    ? "Nothing matches these filters."
    : "Nothing saved yet.";
}

function libraryTile(record) {
  const tile = document.createElement("article");
  tile.className = "tile";
  tile.dataset.id = record.id;
  tile.setAttribute("aria-label", `${record.camera}, ${fullTime(record.stamp)}`);

  const thumb = document.createElement("div");
  thumb.className = "tile-thumb";
  if (record.image) {
    const img = document.createElement("img");
    img.src = record.thumb || record.image;
    img.alt = "";
    img.loading = "lazy";
    thumb.append(img);
  } else {
    const ph = document.createElement("div");
    ph.className = "placeholder";
    ph.textContent = record.clip ? "clip only" : "no image";
    thumb.append(ph);
  }
  if (record.clip) {
    const play = document.createElement("span");
    play.className = "play";
    play.title = "Clip";
    play.append(icon("play"));
    thumb.append(play);
  }
  thumb.append(pill(clockTime(record.stamp), "tile-time"));

  const foot = document.createElement("div");
  foot.className = "tile-foot";
  const name = document.createElement("span");
  name.className = "tile-name";
  name.textContent = record.camera;
  if (record.who && record.who.length) {
    const who = document.createElement("span");
    who.className = record.who_confidence === "confirmed" ? "tl-who sure" : "tl-who";
    who.textContent = record.who.join(" & ");
    name.append(who);
  }
  const text = document.createElement("span");
  text.className = record.summary ? "tile-summary" : "tile-summary empty";
  text.textContent =
    record.summary ||
    (record.state === "describing" ? "Describing…" : "No description");
  foot.append(name, text);

  tile.append(thumb, foot);
  // Looked up rather than baked in: a capture arriving live is inserted at the
  // front, which shifts every position after it.
  activatable(tile, () => openViewer(indexOf(record.id)));
  return tile;
}

function indexOf(id) {
  return lib.items.findIndex((item) => item.id === id);
}

// A capture that lands while the archive is open belongs at the top of it.
// Inserting one beats rebuilding: the pages already loaded stay loaded, and
// nobody reading page four is thrown back to page one.
function prependLibrary(record) {
  const grid = el("library-grid");
  const first = lib.items[0];
  if (!first) {
    appendLibrary([record]);
    return;
  }

  const tile = libraryTile(record);
  if (dayKey(record.stamp) === dayKey(first.stamp)) {
    // Today's heading is already there; the tile goes directly under it.
    grid.insertBefore(tile, grid.children[1] || null);
  } else {
    const heading = document.createElement("h3");
    heading.className = "day";
    heading.textContent = dayLabel(record.stamp);
    grid.prepend(tile);
    grid.prepend(heading);
  }

  lib.items.unshift(record);
  // Everything the viewer is tracking just moved down one.
  if (lib.viewing !== null) lib.viewing += 1;
  el("library-empty").hidden = true;
  renderLibraryFooter();
}

function renderLibraryFooter() {
  el("library-more").hidden = !lib.next;
  const end = el("library-end");
  end.hidden = Boolean(lib.next) || !lib.items.length;
  end.textContent = `${lib.items.length} shown — that is everything`;
  el("filter-clear").hidden = !hasFilters();
}

function hasFilters() {
  return !sameFilters(lib.filters, NO_FILTERS);
}

function setFilterControls() {
  // A camera the archive has not been asked about yet still has to show as
  // chosen; loadStats replaces this list with the real one.
  const select = el("filter-camera");
  const camera = lib.filters.camera;
  if (camera && ![...select.options].some((option) => option.value === camera)) {
    const option = document.createElement("option");
    option.value = camera;
    option.textContent = camera;
    select.append(option);
  }
  select.value = camera;
  el("filter-kind").value = lib.filters.kind;
  el("filter-subject").value = lib.filters.subject;
  el("filter-search").value = lib.filters.q;
  el("filter-until").value = lib.filters.until;
  el("filter-clear").hidden = !hasFilters();
}

// Re-running the whole query on every keystroke would be one request per
// letter, so the search box settles first.
let searchTimer = null;

function applyFilters() {
  clearTimeout(searchTimer);
  lib.filters.camera = el("filter-camera").value;
  lib.filters.kind = el("filter-kind").value;
  lib.filters.subject = el("filter-subject").value;
  lib.filters.q = el("filter-search").value.trim();
  lib.filters.until = el("filter-until").value;
  el("filter-clear").hidden = !hasFilters();
  syncHistory("replace");
  reloadLibrary();
}

function clearFilters() {
  lib.filters = { ...NO_FILTERS };
  setFilterControls();
  syncHistory("replace");
  reloadLibrary();
}

// ------------------------------------------------------------------- viewer

function openViewer(index) {
  const record = lib.items[index];
  if (!record) return;
  lib.viewing = index;

  el("viewer-camera").textContent = record.camera;
  el("viewer-stamp").textContent = fullTime(record.stamp);
  el("viewer-pos").textContent = `${index + 1} of ${lib.items.length}${lib.next ? "+" : ""}`;

  const img = el("viewer-image");
  const video = el("viewer-video");

  if (record.clip) {
    // The clip is the whole point of a motion capture; the still is only its
    // poster frame, so the video goes straight on screen ready to play.
    // Assigning src reloads the video even when the URL has not changed, so a
    // description landing mid-playback would otherwise restart the clip.
    if (video.getAttribute("src") !== record.clip) {
      video.pause();
      video.src = record.clip;
      if (record.image) video.poster = record.image;
      else video.removeAttribute("poster");
    }
    video.hidden = false;
    img.hidden = true;
    img.removeAttribute("src");
  } else {
    video.pause();
    video.removeAttribute("src");
    video.hidden = true;
    img.src = record.image || "";
    img.alt = `${record.camera} at ${fullTime(record.stamp)}`;
    img.hidden = false;
  }

  const download = el("viewer-download");
  download.href = record.clip || record.image || "#";
  download.hidden = !(record.clip || record.image);

  const summary = el("viewer-summary");
  summary.className = record.summary ? "viewer-summary" : "viewer-summary empty";
  if (record.summary) summary.replaceChildren(...describe(record.summary));
  else summary.textContent = "No description for this capture.";

  renderWho(record);

  el("viewer-prev").disabled = index <= 0;
  el("viewer-next").disabled = index >= lib.items.length - 1 && !lib.next;

  const dialog = el("viewer");
  if (!dialog.open) {
    showModal(dialog);
    // Its own history entry, so Back on a phone closes the viewer instead of
    // leaving the page. The address does not change: there is nothing to
    // link to that the library route does not already say.
    if (!route.applying) history.pushState({ viewer: true }, "", location.hash || "#/library");
  }
}

// ------------------------------------------------------------ known people

// Enrolled people, and their reference counts. Loaded once the library is
// opened, since that is the only place enrolling happens.
const who = { people: [], loading: false };
let whoNoteTimer = null;

async function loadPeople() {
  try {
    const data = await getJSON("/api/people");
    who.people = data.people || [];
  } catch (err) {
    who.people = [];
  }
  if (lib.viewing !== null) renderWho(lib.items[lib.viewing]);
}

// Whether this capture is even a candidate for tagging. A clip of an empty
// yard has nobody in it to name.
function taggable(record) {
  return (record.subjects || []).includes("person");
}

// How many people the model counted, when it said. Two people in one frame
// changes both the wording and what the frame is good for.
function crowded(record) {
  return (record.people_count || 0) > 1;
}

function renderWho(record) {
  const box = el("viewer-who");
  box.hidden = !taggable(record);
  if (box.hidden) return;

  const named = record.who || [];
  const confirmed = record.who_confidence === "confirmed";

  // A guess stays hedged, because the model is matching build and posture on
  // a figure a hundred-odd pixels tall. Somebody saying who it is does not.
  const guess = el("who-guess");
  guess.hidden = named.length === 0;
  guess.className = confirmed ? "who-guess confirmed" : "who-guess";
  // Cleared rather than merely hidden, so a previous capture's name cannot
  // reappear when the next one turns the line back on.
  guess.textContent = "";
  if (named.length) {
    const who_ = named.join(" and ");
    guess.textContent = confirmed
      ? `${who_} — confirmed`
      : `Looks like ${who_}${
          record.who_confidence ? ` (${record.who_confidence} confidence)` : ""
        }`;
  }

  // "Who is this?" is the wrong question of a clip with two people in it.
  el("who-label").textContent = crowded(record)
    ? `Who is in this? (${record.people_count} people)`
    : "Who is this?";

  const people = el("who-people");
  people.replaceChildren(
    ...who.people.map((person) => {
      const button = document.createElement("button");
      button.type = "button";
      const on = named.includes(person.name);
      button.className = on ? "who-pick on" : "who-pick";
      // The count is what makes a second click on the same name visibly do
      // something, rather than looking like nothing happened.
      const count = (person.references || []).length;
      button.textContent = count ? `${person.name} · ${count}` : person.name;
      button.title = crowded(record)
        ? `Note that ${person.name} is in this clip`
        : `Add this frame as an example of ${person.name} (${count} so far)`;
      button.disabled = who.loading;
      button.addEventListener("click", () => tagPerson(person.name, record));
      return button;
    })
  );
  el("who-new").disabled = who.loading;
}

function showWhoNote(text) {
  const note = el("who-note");
  note.textContent = text || "";
  note.hidden = !text;
  clearTimeout(whoNoteTimer);
  if (text) whoNoteTimer = setTimeout(() => (el("who-note").hidden = true), 6000);
}

// Tagging is also how a wrong guess gets corrected: the frame it got wrong
// becomes an example of who it actually was, so the next one is better.
async function tagPerson(name, record) {
  if (who.loading) return;
  who.loading = true;
  showWhoNote("");
  renderWho(record);

  let note = "";
  try {
    const result = await post("/api/people", { name, event: record.id });
    if (result.status !== "ok") {
      showMessage("Could not save that", result.message || "The server said no.");
    } else {
      who.people = result.people || who.people;
      // The server records the confirmation on the capture itself, so take
      // the record back from it rather than guessing at what changed here.
      if (result.event) Object.assign(record, result.event);
      note = result.note || `Saved as an example of ${name}.`;
    }
  } catch (err) {
    showMessage("Could not save that", "The server is unreachable.");
  }

  who.loading = false;
  renderWho(record);
  showWhoNote(note);
}

// Asked for in a dialog of its own rather than window.prompt(), which some
// browsers suppress and which cannot be styled or cancelled cleanly.
function newPerson() {
  const record = lib.viewing === null ? null : lib.items[lib.viewing];
  if (!record) return;
  const dialog = el("name-dialog");
  const input = el("name-input");
  input.value = "";
  dialog.returnValue = "";
  dialog.onclose = () => {
    const name = input.value.trim();
    if (dialog.returnValue === "ok" && name) tagPerson(name, record);
  };
  showModal(dialog);
  input.focus();
}

// Everyone enrolled, with the frames they are recognised by. A frame that
// shows the wrong person teaches the model the wrong thing, and until now
// there was no way to take one back.
async function openPeople() {
  hideMenu();
  renderPeopleList();
  showModal(el("people-dialog"));
  await loadPeople();
  renderPeopleList();
}

function renderPeopleList() {
  const list = el("people-list");
  if (!who.people.length) {
    const empty = document.createElement("p");
    empty.className = "people-empty";
    empty.textContent =
      "Nobody is enrolled yet. Open a capture with a person in it in the " +
      "library and say who it is.";
    list.replaceChildren(empty);
    return;
  }

  list.replaceChildren(
    ...who.people.map((person) => {
      const box = document.createElement("section");
      box.className = "person";

      const head = document.createElement("div");
      head.className = "person-head";
      const name = document.createElement("strong");
      name.textContent = person.name;
      const count = document.createElement("span");
      const n = person.references.length;
      count.textContent = `${n} example${n === 1 ? "" : "s"}`;
      const forget = document.createElement("button");
      forget.type = "button";
      forget.className = "btn danger";
      forget.textContent = "Forget";
      forget.title = `Forget ${person.name} and every example of them`;
      confirmClick(forget, "Forget them?", () =>
        forgetPerson(person.slug, null, `Forgot ${person.name}.`)
      );
      head.append(name, count, forget);

      const refs = document.createElement("div");
      refs.className = "refs";
      for (const ref of person.references) {
        const item = document.createElement("figure");
        item.className = "ref";
        item.style.margin = "0";
        const img = document.createElement("img");
        img.src = ref.url;
        img.alt = `Example of ${person.name}`;
        img.loading = "lazy";
        if (ref.added) img.title = `Added ${fullTime(ref.added)}`;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.setAttribute("aria-label", `Remove this example of ${person.name}`);
        remove.title = "Remove this example";
        remove.append(icon("close"));
        confirmClick(remove, "Remove?", () =>
          forgetPerson(person.slug, ref.file, "Example removed.")
        );
        item.append(img, remove);
        refs.append(item);
      }
      box.append(head, refs);
      return box;
    })
  );
}

async function forgetPerson(slug, reference, done) {
  const query = reference ? `?reference=${encodeURIComponent(reference)}` : "";
  try {
    const response = await fetch(`/api/people/${encodeURIComponent(slug)}${query}`, {
      method: "DELETE",
    });
    if (!response.ok) throw new Error(response.statusText);
    const result = await response.json();
    who.people = result.people || [];
    showMessage(done, "");
  } catch (err) {
    showMessage("Could not remove that", "The server is unreachable or said no.");
  }
  renderPeopleList();
  if (lib.viewing !== null) renderWho(lib.items[lib.viewing]);
}

// A destructive button asks twice: the first press turns it into the
// question, the second within a few seconds answers it. No modal needed, and
// a stray tap does nothing.
function confirmClick(button, question, action) {
  let timer = null;
  const original = [...button.childNodes];
  button.addEventListener("click", () => {
    if (button.classList.contains("armed")) {
      clearTimeout(timer);
      button.disabled = true;
      action();
      return;
    }
    button.classList.add("armed");
    button.replaceChildren(question);
    timer = setTimeout(() => {
      button.classList.remove("armed");
      button.replaceChildren(...original);
    }, 3500);
  });
}

async function stepViewer(delta) {
  if (lib.viewing === null) return;
  const target = lib.viewing + delta;
  if (target < 0) return;
  // Walking off the end of what is loaded pulls the next page in rather than
  // stopping at an arbitrary boundary.
  if (target >= lib.items.length) {
    if (!lib.next) return;
    await loadLibraryPage();
    if (target >= lib.items.length) return;
  }
  openViewer(target);
}

// Closing by hand goes back through history, so the entry openViewer added
// is used up rather than left for Back to land on a closed viewer.
function closeViewer() {
  if (lib.viewing === null || lib.closing) return;
  if (history.state && history.state.viewer) {
    lib.closing = true;
    history.back();
  } else {
    closeViewerNow();
  }
}

// Closing on the way somewhere else - the library shutting, a sign-in - just
// shuts it, and neutralises its history entry instead of racing a back().
function closeViewerNow() {
  const video = el("viewer-video");
  video.pause();
  video.removeAttribute("src");
  lib.viewing = null;
  lib.closing = false;
  const dialog = el("viewer");
  closeDialog(dialog);
  if (history.state && history.state.viewer && !route.applying) {
    history.replaceState(null, "", location.hash);
  }
}

// ------------------------------------------------------------------- toasts

// Motion the page did not jump to, and anything worth saying, as a stack of
// popups in a corner. A top-layer popover where the browser has them, so one
// raised while the viewer is open is drawn over it instead of underneath.
const toastBox = el("toasts");
const popovers = typeof toastBox.showPopover === "function";
if (!popovers) toastBox.hidden = true;

const toasts = {
  items: new Map(),     // key -> { node, timer, ms, eventId, clips }
  dismissed: new Set(), // motion events someone already dealt with
  max: 4,
};

function revealToasts() {
  rehomeToasts();
  if (popovers) {
    if (!toastBox.matches(":popover-open")) toastBox.showPopover();
  } else {
    toastBox.hidden = false;
  }
}

// A modal dialog makes everything outside it inert: drawn, but not clickable.
// So the stack lives inside whichever modal is on top - the viewer, usually -
// and back in the page once none is. Moving it takes it out of the top layer,
// so it is shown again, which also puts it above the dialog it moved into.
function rehomeToasts() {
  const modal = [...document.querySelectorAll("dialog[open]")]
    .filter((dialog) => dialog.matches(":modal"))
    .pop();
  const host = modal || document.body;
  if (toastBox.parentElement === host) return;
  host.append(toastBox);
  if (popovers && toasts.items.size && !toastBox.matches(":popover-open")) {
    toastBox.showPopover();
  }
}

function showModal(dialog) {
  dialog.showModal();
  rehomeToasts();
}

// The close event would rehome the popups too, but browsers are allowed to
// deliver it late - Chrome holds it back in a background tab - and popups
// left inside a closed dialog are not drawn at all.
function closeDialog(dialog, value) {
  if (dialog.open) dialog.close(value);
  rehomeToasts();
}

function armToast(key) {
  const item = toasts.items.get(key);
  if (!item) return;
  clearTimeout(item.timer);
  item.timer = setTimeout(() => dismissToast(key), item.ms);
}

// Add a popup under this key, or replace the one already there. The newest
// sits nearest the corner, and the oldest goes when there are too many.
function placeToast(key, node, ms, extra = {}) {
  const old = toasts.items.get(key);
  if (old) {
    clearTimeout(old.timer);
    old.node.remove();
    toasts.items.delete(key);
  }
  toastBox.append(node);
  toasts.items.set(key, { node, timer: null, ms, ...extra });
  // Reading one should not have it vanish mid-sentence.
  node.addEventListener("mouseenter", () => clearTimeout(toasts.items.get(key)?.timer));
  node.addEventListener("mouseleave", () => armToast(key));
  armToast(key);
  while (toasts.items.size > toasts.max) dismissToast(toasts.items.keys().next().value);
  revealToasts();
}

function dismissToast(key) {
  const item = toasts.items.get(key);
  if (!item) return;
  clearTimeout(item.timer);
  item.node.remove();
  toasts.items.delete(key);
  if (item.eventId) toasts.dismissed.add(item.eventId);
  if (!toasts.items.size) {
    if (popovers) {
      if (toastBox.matches(":popover-open")) toastBox.hidePopover();
    } else {
      toastBox.hidden = true;
    }
  }
}

function closeButton(key, label) {
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", label);
  close.append(icon("close"));
  close.addEventListener("click", (e) => {
    e.stopPropagation();
    dismissToast(key);
  });
  return close;
}

// One popup per camera: a burst of clips updates it rather than stacking a
// card per clip, and says how many there have been. Its description fills in
// when the model answers. Clicking it opens that clip.
function showToast(event) {
  const key = `motion:${event.camera}`;
  const held = toasts.items.get(key);
  // Dismissed already - by hand, or by opening the camera - so the
  // description arriving is no reason to bring it back.
  if (toasts.dismissed.has(event.id) && (!held || held.eventId !== event.id)) return;

  if (held && held.eventId === event.id) {
    // The description landing on the popup already showing: in place, so
    // it does not replay its entrance, and with its timer started afresh.
    fillMotionToast(held.node, event, held.clips);
    held.node.onclick = () => openFromToast(key, event);
    armToast(key);
    return;
  }

  const clips = held ? held.clips + 1 : 1;
  const node = document.createElement("div");
  node.className = "toast motion";
  node.tabIndex = 0;
  node.setAttribute("role", "button");
  fillMotionToast(node, event, clips);
  node.onclick = () => openFromToast(key, event);
  node.addEventListener("keydown", (e) => {
    if (e.target === node && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      node.onclick();
    }
  });
  placeToast(key, node, 30000, { eventId: event.id, clips });
}

function fillMotionToast(node, event, clips) {
  const picture = event.thumb || event.image;
  const thumb = document.createElement(picture ? "img" : "span");
  thumb.className = "toast-thumb";
  if (picture) {
    thumb.src = picture;
    thumb.alt = "";
  } else {
    thumb.append(icon("play"));
  }

  const body = document.createElement("div");
  body.className = "toast-body";
  // The camera gets a line to itself: it is the one thing a glance needs.
  const title = document.createElement("strong");
  title.textContent = event.camera;
  const when = document.createElement("time");
  when.dateTime = event.stamp;
  when.textContent =
    clips > 1 ? `${clockTime(event.stamp)} · ${clips} clips` : clockTime(event.stamp);

  const text = document.createElement("span");
  text.className = event.summary ? "toast-text" : "toast-text pending";
  if (event.summary) text.append(...describe(event.summary));
  else text.textContent = "Motion - describing…";

  body.append(title, text, when);
  node.setAttribute("aria-label", `Motion on ${event.camera}. Open it.`);
  node.replaceChildren(thumb, body, closeButton(`motion:${event.camera}`, "Dismiss"));
}

function openFromToast(key, event) {
  dismissToast(key);
  transition(() => isolate(event.camera, event));
}

function showMessage(title, detail) {
  const key = `message:${title}`;
  const node = document.createElement("div");
  node.className = "toast message";
  const body = document.createElement("div");
  body.className = "toast-body";
  const head = document.createElement("strong");
  head.textContent = title;
  body.append(head);
  if (detail) {
    const text = document.createElement("span");
    text.className = "toast-text";
    text.textContent = detail;
    body.append(text);
  }
  node.append(body, closeButton(key, "Dismiss"));
  node.addEventListener("click", () => dismissToast(key));
  placeToast(key, node, 8000);
}

// ---------------------------------------------------------------- the chime

// A soft bell, synthesised rather than shipped as a file: a sine at A5 with
// two quieter, inharmonic partials - the ratios a small bell actually has -
// each fading on its own curve. Quiet, short, and not an alarm.
const chime = { ctx: null, last: 0 };

function audioContext() {
  if (!chime.ctx) {
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) return null;
    chime.ctx = new Context();
  }
  return chime.ctx;
}

function ding(force = false) {
  if (!prefs.sound && !force) return;
  // Several cameras tripping together get one chime, not a chord.
  const now = Date.now();
  if (!force && now - chime.last < 2500) return;
  chime.last = now;

  const ctx = audioContext();
  if (!ctx) return;
  // Browsers start audio suspended until the page has been interacted with.
  // Resuming here works once that has happened; before it, this is silent.
  if (ctx.state === "suspended") ctx.resume().catch(() => {});

  const start = ctx.currentTime + 0.01;
  const master = ctx.createGain();
  master.gain.value = 0.16;
  master.connect(ctx.destination);
  for (const [ratio, level, decay] of [
    [1, 1, 1.6],
    [2.76, 0.28, 0.7],
    [5.4, 0.1, 0.35],
  ]) {
    const osc = ctx.createOscillator();
    osc.type = "sine";
    osc.frequency.value = 880 * ratio;
    const gain = ctx.createGain();
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(level, start + 0.008);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + decay);
    osc.connect(gain);
    gain.connect(master);
    osc.start(start);
    osc.stop(start + decay + 0.05);
  }
}

// The audio context may only start after someone has touched the page, so
// the first touch unlocks it - otherwise a reloaded wall display would stay
// silent until someone happened to open the settings.
function unlockAudio() {
  if (prefs.sound) {
    const ctx = audioContext();
    if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
  }
}
for (const type of ["pointerdown", "keydown"]) {
  document.addEventListener(type, unlockAudio, { capture: true, passive: true });
}

// ------------------------------------------------------------------ helpers

function shortTime(stamp) {
  if (!stamp) return "";
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return stamp;
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// "Today 5:02 PM", "Yesterday 1:39 PM", "Tue 8:10 AM", then a date: what
// someone glancing at a list of events actually wants to know first.
function friendlyTime(stamp) {
  if (!stamp) return "";
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return stamp;
  const clock = date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const days = Math.round((startOfDay(new Date()) - startOfDay(date)) / 86400000);
  if (days === 0) return `Today ${clock}`;
  if (days === 1) return `Yesterday ${clock}`;
  if (days > 1 && days < 7) {
    return `${date.toLocaleDateString([], { weekday: "short" })} ${clock}`;
  }
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

function clockTime(stamp) {
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return stamp || "";
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function fullTime(stamp) {
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return stamp || "";
  return date.toLocaleString([], {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

// A date input speaks in local calendar days; the archive speaks in instants.
function localDate(stamp) {
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

// The last instant of a local calendar day, as the archive's ISO stamp.
function endOfDay(day) {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year, month - 1, date, 23, 59, 59, 999).toISOString();
}

// Day headings are grouped in the viewer's local time, which is what someone
// scrolling the archive is actually thinking in.
function dayKey(stamp) {
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return String(stamp);
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

function dayLabel(stamp) {
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return String(stamp);
  const today = dayKey(new Date().toISOString());
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);
  if (dayKey(stamp) === today) return "Today";
  if (dayKey(stamp) === dayKey(yesterday.toISOString())) return "Yesterday";
  return date.toLocaleDateString([], {
    weekday: "long",
    month: "long",
    day: "numeric",
    year: date.getFullYear() === new Date().getFullYear() ? undefined : "numeric",
  });
}

function humanBytes(size) {
  let value = Number(size) || 0;
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (value < 1024 || unit === "GB") {
      return unit === "B" ? `${Math.round(value)} B` : `${value.toFixed(1)} ${unit}`;
    }
    value /= 1024;
  }
  return `${value.toFixed(1)} GB`;
}

function ageSeconds(stamp) {
  const then = new Date(stamp);
  if (Number.isNaN(then.getTime())) return 0;
  return Math.max(0, Math.round((Date.now() - then.getTime()) / 1000));
}

function relAge(stamp) {
  const seconds = ageSeconds(stamp);
  if (seconds < 90) return "just now";
  if (seconds < 5400) return `${Math.round(seconds / 60)}m old`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)}h old`;
  return `${Math.round(seconds / 86400)}d old`;
}

function cssEscape(value) {
  return (window.CSS && CSS.escape) ? CSS.escape(value) : value.replace(/"/g, '\\"');
}

function setStatus(text, cls) {
  el("status").className = `status ${cls}`;
  el("status-text").textContent = text;
}

// A cross-fade between views where the browser can do one. Only for moves
// someone asked for: a camera opened by motion should just be there.
function transition(update) {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!document.startViewTransition || reduce || document.hidden) {
    update();
    return;
  }
  // A transition is abandoned - its promises rejected - when another starts
  // before it finishes, or the page is hidden mid-way. The update still runs
  // either way, so that is not an error worth reporting.
  const shift = document.startViewTransition(update);
  shift.ready.catch(() => {});
  shift.finished.catch(() => {});
  // This one only fails if the update itself threw, which is a real bug.
  shift.updateCallbackDone.catch((err) => console.error(err));
}

// ------------------------------------------------------- last-checked clock

function renderLastCheck() {
  const rel = el("last-check-rel");
  const abs = el("last-check-abs");
  const box = el("last-check");

  if (!state.lastPoll) {
    rel.textContent = "never";
    abs.textContent = "";
    box.classList.remove("stale");
    return;
  }

  const when = new Date(state.lastPoll);
  const seconds = Math.max(0, Math.round((Date.now() - when.getTime()) / 1000));

  if (seconds < 10) rel.textContent = "just now";
  else if (seconds < 90) rel.textContent = `${seconds}s ago`;
  else if (seconds < 5400) rel.textContent = `${Math.round(seconds / 60)}m ago`;
  else rel.textContent = `${Math.round(seconds / 3600)}h ago`;

  abs.textContent = when.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  box.title = `Last completed check for motion: ${when.toLocaleString()}`;

  // A poll is overdue once it is well past its interval - the loop is wedged
  // or Blink is unreachable.
  box.classList.toggle("stale", seconds > state.pollSeconds * 2);
}

setInterval(renderLastCheck, 1000);

// Card ages are baked in when a card renders, and cards can sit untouched for
// hours, so tick the labels over on their own.
setInterval(() => {
  for (const camera of state.cameras.values()) {
    if (!camera.captured) continue;
    const label = document.querySelector(
      `.card[data-name="${cssEscape(camera.name)}"] .age`
    );
    if (!label) continue;
    label.textContent = relAge(camera.captured);
    label.classList.toggle("stale", ageSeconds(camera.captured) > 3600);
  }
}, 30000);

// ---------------------------------------------------------- the tab itself

// Motion seen while this tab was in the background, counted in its title so
// a pinned tab says there is something to look at.
const unseen = new Set();
const TITLE = document.title;

function renderTitle() {
  document.title = unseen.size ? `(${unseen.size}) ${TITLE}` : TITLE;
}

function noticeInBackground(event) {
  if (!document.hidden) return;
  const first = !unseen.has(event.id);
  unseen.add(event.id);
  renderTitle();
  if (!prefs.notify || !("Notification" in window)) return;
  if (Notification.permission !== "granted") return;
  // One notification per event: the description landing later replaces the
  // "describing" one in place instead of buzzing a second time.
  try {
    const note = new Notification(`Motion: ${event.camera}`, {
      body: event.summary || "Describing…",
      tag: event.id,
      icon: event.image || "/static/icon.svg",
      silent: !first,
    });
    note.onclick = () => {
      window.focus();
      isolate(event.camera, event);
      note.close();
    };
  } catch (err) {
    // Android only allows these from a service worker; the title still counts.
  }
}

// The screen of a wall display should stay on while the page is open. The
// lock is dropped whenever the page is hidden and has to be asked for again.
let wakeLock = null;

async function applyWakeLock() {
  if (!("wakeLock" in navigator)) return;
  if (prefs.wake && document.visibilityState === "visible") {
    if (wakeLock) return;
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => {
        wakeLock = null;
      });
    } catch (err) {
      wakeLock = null;
    }
  } else if (wakeLock) {
    wakeLock.release().catch(() => {});
    wakeLock = null;
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  unseen.clear();
  renderTitle();
  applyWakeLock();
  // A phone that slept for an hour has a socket that died with it. Waiting
  // out the retry timer would show an hour-old page for a few more seconds.
  reconnectNow();
});
window.addEventListener("online", reconnectNow);

// -------------------------------------------------------------------- socket

function applyEvent(event) {
  // Both timelines read out of these lists, so they are brought up to date
  // before anything decides what to show.
  mergeIntoFeeds(event);
  renderTimeline();
  noticeInBackground(event);

  // Redrawn even while that camera is open in the detail view: the card
  // carries the description, and returning to the grid must not find it
  // still saying "Describing…".
  const camera = state.cameras.get(event.camera);
  if (camera) {
    camera.last_event = event;
    updateCard(camera);
  }

  // Flash the card even if we are not switching to it.
  const card = document.querySelector(`.card[data-name="${cssEscape(event.camera)}"]`);
  if (card) {
    card.classList.add("alert");
    setTimeout(() => card.classList.remove("alert"), 4500);
  }

  // Never yank someone out of the archive they are reading; the toast is
  // still there if they want to jump to the new event.
  const following = el("follow").checked && !lib.open;
  if (following) {
    isolate(event.camera, event);
  } else if (state.selected === event.camera && !lib.open) {
    // Already looking at this camera: refresh in place.
    isolate(event.camera, event);
  } else {
    showToast(event);
  }
}

// A capture that arrives while the library is open belongs at the top of it,
// and a description landing later belongs on the tile already there.
function mergeIntoLibrary(record) {
  const existing = indexOf(record.id);
  if (existing >= 0) {
    lib.items[existing] = { ...lib.items[existing], ...record };
    const tile = document.querySelector(
      `.tile[data-id="${cssEscape(record.id)}"]`
    );
    if (tile) tile.replaceWith(libraryTile(lib.items[existing]));
    if (lib.viewing === existing) openViewer(existing);
    return;
  }

  // Something that the current filters would hide changes nothing on screen,
  // and changing the filters refetches anyway.
  if (!matchesFilters(record)) return;

  if (!lib.open) {
    // Nothing to redraw while the view is closed, so just remember that what
    // it holds is now behind the archive.
    lib.stale = true;
    return;
  }
  prependLibrary(record);
  loadStats();
}

function matchesFilters(record) {
  const { camera, kind, subject, q, until } = lib.filters;
  if (camera && record.slug !== camera) return false;
  if (kind && record.kind !== kind) return false;
  if (subject && !(record.subjects || []).includes(subject)) return false;
  if (until && new Date(record.stamp) > new Date(endOfDay(until))) return false;
  if (q) {
    const needle = q.toLowerCase();
    const summary = (record.summary || "").toLowerCase();
    if (!summary.includes(needle) && !record.camera.toLowerCase().includes(needle)) {
      return false;
    }
  }
  return true;
}

let socket = null;
let reconnectTimer = null;

function connect() {
  clearTimeout(reconnectTimer);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  socket = ws;

  ws.onopen = () => setStatus("watching", "live");
  ws.onclose = () => {
    // A socket that was already replaced has nothing left to retry.
    if (socket !== ws) return;
    setStatus("reconnecting", "down");
    reconnectTimer = setTimeout(connect, 3000);
  };
  ws.onerror = () => ws.close();

  ws.onmessage = (msg) => {
    const data = JSON.parse(msg.data);

    if (data.type === "sync") {
      Object.assign(sync, data.sync);
      renderSync();
      return;
    }

    if (data.type === "ntfy") {
      state.ntfy = data.ntfy;
      renderNtfy();
      return;
    }

    if (data.type === "state") {
      state.ntfy = data.ntfy || null;
      renderNtfy();
      state.cameras = new Map(data.cameras.map((c) => [c.name, c]));
      state.lastPoll = data.last_poll;
      state.pollSeconds = data.poll_seconds || 60;
      state.pageSize = data.page_size || state.pageSize;
      state.timeline = data.timeline || state.timeline;
      state.events = (data.events || []).slice(0, state.timeline);
      // Anything captured while the socket was down was broadcast to nobody,
      // so every fetched list has to come off the archive again, and the
      // library too.
      state.feeds.clear();
      if (state.peopleOnly) loadTimelineFeed("");
      lib.stale = true;
      applyStatus(data.status, data.message);
      renderGrid();
      renderTimeline();
      renderLastCheck();
      if (data.status === "watching") {
        setStatus(`${data.cameras.length} cameras`, "live");
      }
      if (route.pending) {
        const pending = route.pending;
        route.pending = null;
        applyRoute(pending, history.state);
      } else if (state.selected) {
        isolate(state.selected, null, state.showingLive);
      }
      return;
    }

    if (data.type === "status") {
      applyStatus(data.status, data.message);
      return;
    }

    if (data.type === "poll") {
      state.lastPoll = data.last_poll;
      state.pollSeconds = data.poll_seconds || state.pollSeconds;
      renderLastCheck();
      if (data.status === "error") {
        setStatus("check failed", "down");
      } else {
        setStatus(`${state.cameras.size} cameras`, "live");
      }
      return;
    }

    if (data.type === "snapshot") {
      state.cameras.set(data.camera.name, data.camera);
      // Only refresh a live still; never yank a clip out from under someone.
      if (
        state.selected === data.camera.name &&
        !state.showingClip &&
        (state.showingLive || !data.camera.last_event)
      ) {
        isolate(data.camera.name, null, state.showingLive);
      }
      updateCard(data.camera);
      return;
    }

    if (data.type === "capture") {
      // A newly saved still: nothing to isolate, but the library should show
      // it without a reload.
      mergeIntoLibrary(data.event);
      return;
    }

    if (data.type === "motion" || data.type === "summary") {
      // Once per clip, when it is first seen: a description arriving later
      // is not new motion, and neither is a clip from weeks ago.
      if (data.type === "motion" && !data.backfill) ding();
      mergeIntoLibrary(data.event);
      if (data.backfill) updateEventInPlace(data.event);
      else applyEvent(data.event);
    }
  };
}

function reconnectNow() {
  if (!socket || socket.readyState === WebSocket.CLOSING || socket.readyState === WebSocket.CLOSED) {
    connect();
  }
}

// ------------------------------------------------------------------- routing

// The view is mirrored in the address - #/, #/camera/<slug>, #/library with
// its filters - so Back and Forward move between views, a reload lands where
// it was, and a camera or a filtered search can be bookmarked or sent on.
const route = {
  applying: false,  // replaying history; do not write it back
  pending: null,    // a camera route waiting for the camera list to arrive
};

function currentHash() {
  if (lib.open) {
    const params = new URLSearchParams();
    for (const key of Object.keys(NO_FILTERS)) {
      if (lib.filters[key]) params.set(key, lib.filters[key]);
    }
    const query = params.toString();
    return query ? `#/library?${query}` : "#/library";
  }
  if (state.selected) {
    const camera = state.cameras.get(state.selected);
    return `#/camera/${encodeURIComponent(camera ? camera.slug : state.selected)}`;
  }
  return "#/";
}

function syncHistory(mode) {
  if (route.applying) return;
  const hash = currentHash();
  if (hash === (location.hash || "#/")) return;
  if (mode === "replace") history.replaceState(null, "", hash);
  else history.pushState(null, "", hash);
}

function parseRoute(hash) {
  const raw = (hash || "").replace(/^#\/?/, "");
  const [path, query] = raw.split("?");
  const parts = path.split("/").filter(Boolean);
  if (parts[0] === "library") {
    return { view: "library", params: new URLSearchParams(query || "") };
  }
  if (parts[0] === "camera" && parts[1]) {
    return { view: "camera", slug: decodeURIComponent(parts[1]) };
  }
  return { view: "home" };
}

// Only values the controls could have produced: a hand-edited address must
// not send the server a kind or subject it will refuse.
function filtersFrom(params) {
  const allowed = (id, value) =>
    [...el(id).options].some((option) => option.value === value) ? value : "";
  const until = params.get("until") || "";
  return {
    camera: params.get("camera") || "",
    kind: allowed("filter-kind", params.get("kind") || ""),
    subject: allowed("filter-subject", params.get("subject") || ""),
    q: params.get("q") || "",
    until: /^\d{4}-\d{2}-\d{2}$/.test(until) ? until : "",
  };
}

function applyRoute(target, historyState) {
  route.applying = true;
  try {
    if (lib.viewing !== null && !(historyState && historyState.viewer)) {
      closeViewerNow();
    }
    if (target.view === "library") {
      const filters = filtersFrom(target.params);
      if (!sameFilters(filters, lib.filters)) {
        lib.filters = filters;
        setFilterControls();
        if (lib.open) reloadLibrary();
        else lib.stale = true;
      }
      if (!lib.open) openLibrary();
    } else if (target.view === "camera") {
      const camera = [...state.cameras.values()].find((c) => c.slug === target.slug);
      if (camera) {
        isolate(camera.name);
      } else if (!state.cameras.size) {
        // Nothing to open yet: the camera list arrives with the socket.
        route.pending = target;
      } else {
        showMessage("No such camera", `Nothing called "${target.slug}" here now.`);
        if (lib.open) closeLibrary();
        else if (state.selected) backToGrid();
        history.replaceState(null, "", "#/");
      }
    } else if (lib.open) {
      closeLibrary();
    } else if (state.selected) {
      backToGrid();
    }
  } finally {
    route.applying = false;
  }
}

window.addEventListener("popstate", (e) => {
  const closing = lib.closing;
  lib.closing = false;
  // A viewer entry only means "viewer open" while one is. Landing on one
  // any other way - Forward after closing it, or overshooting on the way
  // out - leaves a marker for a later close to trip over, so it is cleared.
  let marker = Boolean(e.state && e.state.viewer);
  if (marker && (closing || lib.viewing === null)) {
    history.replaceState(null, "", location.hash);
    marker = false;
  }
  applyRoute(parseRoute(location.hash), marker ? e.state : null);
});

// ------------------------------------------------------------------ settings

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "light" || theme === "dark") root.dataset.theme = theme;
  else delete root.dataset.theme;
  // The browser's own chrome - the address bar on a phone - matches too.
  const color = getComputedStyle(root).getPropertyValue("--bg").trim();
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta && color) meta.content = color;
}

function setupSettings() {
  const menu = el("settings");
  const button = el("open-settings");

  // Placed under its button each time it opens: the header wraps on a
  // narrow screen, so there is no one place it always is.
  const place = () => {
    const rect = button.getBoundingClientRect();
    menu.style.top = `${Math.round(rect.bottom + 8)}px`;
    menu.style.right = `${Math.max(8, Math.round(window.innerWidth - rect.right))}px`;
  };
  if (typeof menu.showPopover === "function") {
    menu.addEventListener("beforetoggle", (e) => {
      if (e.newState === "open") place();
    });
  } else {
    // No popover support: a plain toggle, closed by clicking elsewhere.
    menu.hidden = true;
    button.addEventListener("click", (e) => {
      e.stopPropagation();
      place();
      menu.hidden = !menu.hidden;
    });
    document.addEventListener("click", (e) => {
      if (!menu.contains(e.target)) menu.hidden = true;
    });
  }

  for (const radio of document.querySelectorAll('input[name="theme"]')) {
    radio.checked = radio.value === prefs.theme;
    radio.addEventListener("change", () => {
      savePref("theme", radio.value);
      applyTheme(radio.value);
    });
  }
  window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () =>
    applyTheme(prefs.theme)
  );
  applyTheme(prefs.theme);

  // Both need a secure context - localhost, or HTTPS - and are simply not
  // offered when the page is served over plain HTTP on the LAN.
  const sound = el("pref-sound");
  sound.checked = prefs.sound;
  sound.addEventListener("change", () => {
    savePref("sound", sound.checked);
    // Turning it on is a click, which is what lets audio start - and a
    // sample of what it will sound like.
    if (sound.checked) ding(true);
  });

  if ("Notification" in window && window.isSecureContext) {
    el("notify-row").hidden = false;
    const box = el("pref-notify");
    box.checked = prefs.notify && Notification.permission === "granted";
    box.addEventListener("change", async () => {
      if (box.checked && Notification.permission !== "granted") {
        const answer = await Notification.requestPermission();
        if (answer !== "granted") {
          box.checked = false;
          showMessage(
            "Desktop alerts are blocked",
            "Allow notifications for this site in the browser's settings."
          );
        }
      }
      savePref("notify", box.checked);
    });
  }

  if ("wakeLock" in navigator) {
    el("wake-row").hidden = false;
    const box = el("pref-wake");
    box.checked = prefs.wake;
    box.addEventListener("change", () => {
      savePref("wake", box.checked);
      applyWakeLock();
    });
    applyWakeLock();
  }

  if (document.fullscreenEnabled) {
    const full = el("go-fullscreen");
    full.hidden = false;
    full.addEventListener("click", () => {
      hideMenu();
      toggleFullscreen();
    });
  }

  setupNtfy();
  el("open-people").addEventListener("click", openPeople);
  el("open-help").addEventListener("click", openHelp);
}

// ------------------------------------------------------- push notifications

// Unlike everything else in the menu this is not a browser preference: the
// server sends the notifications, so a pause here is a pause for everyone.
// Only the length offered for the next pause is remembered per browser.
let ntfyTimer = null;

function setupNtfy() {
  const box = el("ntfy-on");
  const hours = el("ntfy-hours");
  hours.value = prefs.ntfyHours;
  if (hours.selectedIndex < 0) hours.value = "8";

  box.addEventListener("change", () => {
    setNtfy(box.checked ? { paused: false } : pauseRequest());
  });
  hours.addEventListener("change", () => {
    savePref("ntfyHours", hours.value);
    // Already paused: the new length counts from now, which is what someone
    // picking "2 hours" means whatever was chosen before.
    if (state.ntfy && state.ntfy.paused) setNtfy(pauseRequest());
  });
}

function pauseRequest() {
  const value = el("ntfy-hours").value;
  return value ? { paused: true, hours: Number(value) } : { paused: true };
}

async function setNtfy(payload) {
  const box = el("ntfy-on");
  box.disabled = true;
  try {
    const response = await fetch("/api/ntfy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    state.ntfy = await response.json();
  } catch (err) {
    showMessage("Could not change push notifications", "The server is unreachable.");
  } finally {
    box.disabled = false;
    // Puts the switch back where the server says it is if the request failed.
    renderNtfy();
  }
}

function renderNtfy() {
  const info = state.ntfy;
  clearTimeout(ntfyTimer);
  ntfyTimer = null;
  el("ntfy-group").hidden = !(info && info.configured);
  const paused = Boolean(info && info.configured && info.paused);
  el("ntfy-paused").hidden = !paused;
  el("ntfy-on").checked = !paused;

  const note = el("ntfy-note");
  note.classList.toggle("paused", paused);
  if (!paused) {
    note.textContent = "Turning this off pauses them for everyone.";
    return;
  }
  const text = info.until
    ? `Paused until ${friendlyTime(info.until)}`
    : "Paused until turned back on";
  note.textContent = text;
  el("ntfy-paused").title = `Push notifications are ${text.toLowerCase()}`;

  // The server ends the pause by itself and says nothing when it does, so
  // the page keeps its own time rather than show a pause that is over.
  if (info.until) {
    const left = new Date(info.until).getTime() - Date.now();
    ntfyTimer = setTimeout(() => {
      state.ntfy = { ...info, paused: false, until: null };
      renderNtfy();
    }, Math.max(0, left) + 1000);
  }
}

function hideMenu() {
  const menu = el("settings");
  if (typeof menu.hidePopover === "function") {
    if (menu.matches(":popover-open")) menu.hidePopover();
  } else {
    menu.hidden = true;
  }
}

function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else document.documentElement.requestFullscreen().catch(() => {});
}

function openHelp() {
  hideMenu();
  showModal(el("help-dialog"));
}

// -------------------------------------------------------------------- login

function applyStatus(status, message) {
  state.status = status;
  const overlay = el("overlay");

  if (status === "watching") {
    overlay.hidden = true;
    return;
  }
  if (status === "needs-credentials" || status === "awaiting-2fa") {
    // Anything modal would sit over the sign-in form in the top layer.
    closeViewerNow();
    for (const dialog of document.querySelectorAll("dialog[open]")) closeDialog(dialog);
    overlay.hidden = false;
    const needCode = status === "awaiting-2fa";
    el("login-form").hidden = needCode;
    el("tfa-form").hidden = !needCode;
    // Say why we are asking, when the server told us.
    showError("login-error", needCode ? "" : message);
    el(needCode ? "code" : "username").focus();
    setStatus(needCode ? "awaiting code" : "sign in required", "down");
  }
}

function showError(id, message) {
  const box = el(id);
  box.textContent = message || "";
  box.hidden = !message;
}

async function post(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return response.json();
}

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

el("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = el("login-submit");
  button.disabled = true;
  button.textContent = "Signing in…";
  showError("login-error", "");

  try {
    const result = await post("/api/login", {
      username: el("username").value,
      password: el("password").value,
    });
    // Clear the password either way; it lives on the server side now.
    el("password").value = "";

    if (result.status === "2fa_required") {
      applyStatus("awaiting-2fa");
    } else if (result.status === "connected") {
      applyStatus("watching");
    } else {
      showError("login-error", result.message || "Sign-in failed.");
    }
  } catch (err) {
    showError("login-error", "Could not reach the server.");
  } finally {
    button.disabled = false;
    button.textContent = "Sign in";
  }
});

el("tfa-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = el("tfa-submit");
  button.disabled = true;
  button.textContent = "Verifying…";
  showError("tfa-error", "");

  try {
    const result = await post("/api/2fa", { code: el("code").value });
    el("code").value = "";
    if (result.status === "connected") {
      applyStatus("watching");
    } else {
      showError("tfa-error", result.message || "Verification failed.");
    }
  } catch (err) {
    showError("tfa-error", "Could not reach the server.");
  } finally {
    button.disabled = false;
    button.textContent = "Verify";
  }
});

// ------------------------------------------------------------------- wiring

el("back").addEventListener("click", () => transition(backToGrid));
el("detail-prev").addEventListener("click", () => stepCamera(-1));
el("detail-next").addEventListener("click", () => stepCamera(1));
el("stage-play").addEventListener("click", () => el("toggle-media").click());
el("detail-library").addEventListener("click", () => {
  const camera = state.cameras.get(state.selected);
  if (camera) transition(() => openLibraryWith({ camera: camera.slug }));
});

el("tab-cameras").addEventListener("click", () => {
  if (lib.open) transition(() => closeLibrary());
  else if (state.selected) transition(backToGrid);
  else window.scrollTo({ top: 0, behavior: "smooth" });
});
el("open-library").addEventListener("click", () => {
  if (!lib.open) transition(openLibrary);
});
el("library-more").addEventListener("click", loadLibraryPage);
el("filter-camera").addEventListener("change", applyFilters);
el("filter-kind").addEventListener("change", applyFilters);
el("filter-subject").addEventListener("change", applyFilters);
el("filter-until").addEventListener("change", applyFilters);
el("filter-clear").addEventListener("click", clearFilters);
el("describe-missing").addEventListener("click", startBackfill);
el("sync-now").addEventListener("click", startSync);
el("follow").addEventListener("change", (e) => savePref("follow", e.target.checked));
el("timeline-people").addEventListener("change", (e) => setPeopleOnly(e.target.checked));
el("detail-timeline-people").addEventListener("change", (e) =>
  setPeopleOnly(e.target.checked)
);
el("filter-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(applyFilters, 250);
});

el("who-new").addEventListener("click", newPerson);
el("name-cancel").addEventListener("click", () => closeDialog(el("name-dialog"), ""));

el("viewer-close").addEventListener("click", closeViewer);
el("viewer-prev").addEventListener("click", () => stepViewer(-1));
el("viewer-next").addEventListener("click", () => stepViewer(1));
// Clicking the backdrop closes; clicking the media itself must not.
el("viewer").addEventListener("click", (e) => {
  if (e.target === el("viewer")) closeViewer();
});
// Esc on a modal dialog closes it outright; route it through closeViewer so
// the history entry goes with it.
el("viewer").addEventListener("cancel", (e) => {
  e.preventDefault();
  closeViewer();
});
el("viewer").addEventListener("close", () => {
  if (lib.viewing !== null) closeViewerNow();
});

// A horizontal swipe across the picture steps through the archive on a
// touch screen, as the arrow keys do on a keyboard. A touch that starts on
// the video's control bar is scrubbing, not swiping.
(() => {
  const stage = el("viewer-stage");
  let start = null;
  stage.addEventListener(
    "touchstart",
    (e) => {
      if (e.touches.length !== 1) {
        start = null;
        return;
      }
      const touch = e.touches[0];
      const video = el("viewer-video");
      if (!video.hidden && e.target === video) {
        const rect = video.getBoundingClientRect();
        if (touch.clientY > rect.bottom - 64) {
          start = null;
          return;
        }
      }
      start = { x: touch.clientX, y: touch.clientY };
    },
    { passive: true }
  );
  stage.addEventListener(
    "touchend",
    (e) => {
      if (!start) return;
      const touch = e.changedTouches[0];
      const dx = touch.clientX - start.x;
      const dy = touch.clientY - start.y;
      start = null;
      if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) {
        stepViewer(dx < 0 ? 1 : -1);
      }
    },
    { passive: true }
  );
})();

// Whatever closes a dialog, the popups go back to where they can be clicked.
for (const dialog of document.querySelectorAll("dialog")) {
  dialog.addEventListener("close", rehomeToasts);
}

// Every smaller dialog closes from its close button or its backdrop.
for (const dialog of document.querySelectorAll("dialog.sheet")) {
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog) closeDialog(dialog);
  });
  for (const button of dialog.querySelectorAll("[data-close]")) {
    button.addEventListener("click", () => closeDialog(dialog));
  }
}

// Pull the next page in as the bottom of the list comes into view, so
// scrolling the archive does not mean hunting for a button.
new IntersectionObserver(
  (entries) => {
    if (entries.some((entry) => entry.isIntersecting) && lib.open && lib.next) {
      loadLibraryPage();
    }
  },
  { rootMargin: "600px" }
).observe(el("library-more").parentElement);

// Sticky day headings sit under the header, whose height changes as it wraps.
new ResizeObserver(([entry]) => {
  const height = Math.round(entry.target.getBoundingClientRect().height);
  document.documentElement.style.setProperty("--header-h", `${height}px`);
}).observe(document.querySelector(".topbar"));

// ----------------------------------------------------------------- keyboard

function typingIn(target) {
  return Boolean(
    target.closest && target.closest("input, select, textarea, [contenteditable]")
  );
}

document.addEventListener("keydown", (e) => {
  if (e.defaultPrevented || e.isComposing) return;
  // The sign-in form and any smaller dialog handle their own keys.
  if (!el("overlay").hidden) return;
  if (document.querySelector("dialog.sheet[open]")) return;
  const menu = el("settings");
  if (menu.matches && menu.matches(":popover-open")) return;

  // Innermost thing first: the viewer sits on top of the library, which sits
  // in place of the grid.
  if (lib.viewing !== null) {
    if (e.key === "Escape") {
      e.preventDefault();
      closeViewer();
    } else if (e.key === "ArrowLeft") stepViewer(-1);
    else if (e.key === "ArrowRight") stepViewer(1);
    return;
  }

  if (e.key === "Escape") {
    // The first Esc leaves the search box; the next leaves the library.
    if (typingIn(e.target)) {
      e.target.blur();
      return;
    }
    if (lib.open) transition(() => closeLibrary());
    else if (state.selected) transition(backToGrid);
    return;
  }

  if (typingIn(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
  // A focused video seeks with the arrow keys.
  const onVideo = e.target.tagName === "VIDEO";

  const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  const names = [...state.cameras.keys()];

  if (/^[1-9]$/.test(key) && names[Number(key) - 1]) {
    transition(() => isolate(names[Number(key) - 1]));
  } else if (key === "g") {
    if (lib.open) transition(() => closeLibrary());
    else if (state.selected) transition(backToGrid);
  } else if (key === "l") {
    if (!lib.open) transition(openLibrary);
  } else if (key === "/") {
    e.preventDefault();
    if (!lib.open) openLibrary();
    el("filter-search").focus();
  } else if (key === "s") {
    startSync();
  } else if (key === "f") {
    const follow = el("follow");
    follow.checked = !follow.checked;
    savePref("follow", follow.checked);
    showMessage(follow.checked ? "Following motion" : "Not following motion", "");
  } else if (key === "?") {
    openHelp();
  } else if (state.selected && !lib.open) {
    if (key === "ArrowLeft" && !onVideo) stepCamera(-1);
    else if (key === "ArrowRight" && !onVideo) stepCamera(1);
    else if (key === "p" && !el("toggle-media").hidden) el("toggle-media").click();
    else if (key === "r") snap(state.selected);
  }
});

// -------------------------------------------------------------------- start

el("follow").checked = prefs.follow;
state.peopleOnly = prefs.peopleOnly;
el("timeline-people").checked = state.peopleOnly;
el("detail-timeline-people").checked = state.peopleOnly;
setupSettings();

// A reload keeps history.state, including the marker of a viewer that is no
// longer open.
if (history.state && history.state.viewer) history.replaceState(null, "", location.hash);
applyRoute(parseRoute(location.hash), null);

connect();
refreshSync();
